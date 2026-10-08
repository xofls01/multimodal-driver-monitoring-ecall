"""Unity 차량 연동 (팀원 Unity 프로젝트 unity_stage1_basic_vehicle 의 TCP 규격 그대로).

  Unity(TCP 클라이언트) ──vehicle_state 20Hz──▶ driver_monitor(TCP 서버, 포트 5001)
                       ◀─vehicle_control 20Hz──

driver_monitor 가 팀원의 vehicle_tcp_server.py 자리를 대신한다 (같은 포트라 동시에 켜면 안 됨).

동작 (VehicleController)
  CRUISE   정속 주행: 목표 속도 유지 + 차로 중앙 유지                     [A] 수동/정속 전환
  MANUAL   remote_enabled=false → Unity 키보드 운전
  ALERT    의식 저하 DANGER(takeover_reasons)가 takeover_after_s 이어지면 개입 시작.
           비상등 켜고 속도 유지. alert_s 안에 운전자가 깨어나면 취소
  DECEL    단계적 감속 + 갓길 이동 (이후로는 운전자가 깨어나도 계속 진행)
  STOPPED  정차 완료 → e-Call MSD 생성·저장, ecall=true                    [G] 해제 → 정속 주행
  ROAD_END 정속 주행 중 도로 끝 → 차로 안에서 정차 (Unity 에서 R 로 출발점 복귀하면 다시 주행)
  [T] 시험용으로 개입을 바로 시작

판정(카메라)과 차량 제어를 분리해 두었다: 심박 센서가 붙으면 DmsInfo 에 값을 더하고
_takeover_condition() 만 고치면 된다.

e-Call 은 ecall_dir 에 MSD JSON 파일로만 저장한다 (실제 긴급번호·외부 서버로 보내지 않음).
"""
from __future__ import annotations

import json
import math
import socket
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .monitor import DANGER, LEVEL_NAME

MANUAL, CRUISE, ALERT, DECEL, STOPPED, ROAD_END = "MANUAL", "CRUISE", "ALERT", "DECEL", "STOPPED", "ROAD_END"
EMERGENCY_PHASES = (ALERT, DECEL, STOPPED)

# 제어 이득 (가상 차량 모델로 맞춘 값. 실제 Unity 에서 다를 수 있음)
KP_SPEED = 0.06          # throttle / (km/h 오차)
KI_SPEED = 0.03          # throttle / (km/h·s)
KB_SPEED = 0.04          # brake / (km/h 초과)
K_LAT_DEG = 8.0          # 목표 진행 방향(도) / 목표 x 와의 거리(m)  ─ 위치 오차 → 원하는 진행 방향
HEAD_REF_LIMIT = 15.0    # 원하는 진행 방향 최대 각도(도)
K_HEAD = 0.04            # steering / deg (원하는 진행 방향과의 차이)
STEER_LIMIT = 0.5
LANE_CHANGE_S = 3.0      # 목표 x 를 차로 → 갓길로 옮기는 시간
PULL_OVER_MIN_KPH = 15.0 # 갓길 이동이 끝나기 전에는 이 속도 아래로 줄이지 않음 (느리면 옆으로 잘 안 감)
STOP_KPH = 0.5
ALERT_CANCEL_S = 1.0     # ALERT 중 위험이 이만큼 사라지면 취소
STATE_STALE_S = 1.0      # Unity 상태가 이보다 오래되면 없는 것으로 봄
DMS_STALE_S = 0.5        # 판정 결과가 이보다 오래되면 명령을 보내지 않음 → Unity 자체 페일세이프(0.75초 후 제동)


@dataclass
class Command:
    """vehicle_control 의 조작값 (팀원 TCP_PROTOCOL.md 와 같은 필드)."""
    throttle: float = 0.0
    brake: float = 0.0
    steering: float = 0.0
    hazard: bool = False
    ecall: bool = False
    remote_enabled: bool = False


@dataclass
class DmsInfo:
    """판정 결과 중 차량 제어에 쓰는 것."""
    level: int = 0
    reasons: list = field(default_factory=list)
    danger_reasons: list = field(default_factory=list)
    hr_bpm: float = 0.0
    hr_valid: bool = False

    @classmethod
    def from_state(cls, s) -> "DmsInfo":
        return cls(level=s.level, reasons=list(s.reasons), danger_reasons=list(s.danger_reasons),
                   hr_bpm=s.hr, hr_valid=s.hr_valid)


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def heading_error_deg(vs: dict) -> float:
    """+z 방향 기준 진행 방향 오차(도). 오른쪽으로 틀어져 있으면 +."""
    f = vs.get("forward")
    if f:
        return math.degrees(math.atan2(f.get("x", 0.0), f.get("z", 1.0)))
    return (vs.get("heading_deg", 0.0) + 180.0) % 360.0 - 180.0


class VehicleController:
    def __init__(self, cfg):
        self.cfg = cfg
        self.phase = CRUISE
        self.events: list[tuple[str, float]] = []   # (이름, monotonic 시각) — 개입 타임라인
        self.msd: dict | None = None
        self.msd_path: Path | None = None
        self.message = ""
        self._reset_emergency()
        self._i = 0.0
        self._last_t: float | None = None
        self._last_z: float | None = None

    # ---------- 외부 조작 (키) ----------
    def new_session(self, why: str):
        """Unity 를 다시 Play 했거나 R 로 출발점에 돌아옴 → 새 시연. 비상·도로 끝 상태를 풀고 정속 주행."""
        if self.phase in EMERGENCY_PHASES or self.phase == ROAD_END:
            resume = self._resume_phase if self.phase in EMERGENCY_PHASES else CRUISE
            print(f"[vehicle] {why} → {resume}")
            self.phase, self._i, self.message = resume, 0.0, why
            self._reset_emergency()

    def toggle_cruise(self):
        if self.phase in (MANUAL, ROAD_END):
            self.phase, self._i = CRUISE, 0.0
        elif self.phase == CRUISE:
            self.phase = MANUAL

    def resolve(self):
        """비상 상황 해제 → 정속 주행."""
        if self.phase in EMERGENCY_PHASES:
            self._log("resolved", self._last_t or time.monotonic())
            self.phase, self._i = CRUISE, 0.0
            self._reset_emergency()
            self.message = "resolved"

    def trigger_test(self, t: float):
        if self.phase not in EMERGENCY_PHASES:
            self._start_emergency(t, ["TEST TRIGGER"], None, None)

    # ---------- 메인 ----------
    def step(self, t: float, vs: dict | None, dms: DmsInfo | None) -> Command:
        c = self.cfg
        dt = 0.05 if self._last_t is None else _clamp(t - self._last_t, 0.0, 0.5)
        self._last_t = t
        danger = self._takeover_condition(dms)
        if not danger:
            self._danger_since = None
        elif self._danger_since is None:
            self._danger_since = t
        danger_s = 0.0 if self._danger_since is None else t - self._danger_since

        if self.phase in (CRUISE, MANUAL) and danger_s >= c.takeover_after_s:
            self._start_emergency(t, dms.danger_reasons, dms, vs)

        if vs is None:                                  # Unity 상태 없음: 제자리 제동 명령만
            return Command(brake=1.0 if self.phase != MANUAL else 0.0, remote_enabled=self.phase != MANUAL,
                           hazard=self.phase in EMERGENCY_PHASES, ecall=self.phase == STOPPED)

        v = float(vs.get("speed_kph", 0.0))
        pos = vs.get("position") or {}
        x, z = float(pos.get("x", 0.0)), float(pos.get("z", 0.0))
        if self._last_z is not None and abs(z - self._last_z) > 15.0:   # 한 주기에 15m 넘게 이동 = 위치 리셋
            self.new_session("vehicle reset in Unity")
        self._last_z = z

        if self.phase == MANUAL:
            return Command(remote_enabled=False)

        if self.phase == CRUISE:
            if z > c.road_end_z:
                self.phase, self.message = ROAD_END, "end of road: press R in Unity"
                self._log("road_end", t)
            else:
                return self._drive(c.cruise_kph, c.lane_center_x, v, x, vs, dt)

        if self.phase == ROAD_END:
            if z < c.road_end_z - 50.0:                 # Unity 에서 R 로 출발점 복귀
                self.phase, self._i, self.message = CRUISE, 0.0, ""
                return self._drive(c.cruise_kph, c.lane_center_x, v, x, vs, dt)
            return self._drive(0.0, c.lane_center_x, v, x, vs, dt, ramp=True)

        if self.phase == ALERT:
            is_test = bool(self.events) and self.events[0][0] == "test_trigger"
            if danger or is_test:
                self._quiet_since = None
            else:
                if self._quiet_since is None:
                    self._quiet_since = t
                if t - self._quiet_since >= ALERT_CANCEL_S:     # 운전자가 깨어남 → 취소
                    self._log("alert_cancelled", t)
                    self.phase, self.message = self._resume_phase, "driver responded"
                    self._reset_emergency()
                    if self.phase == MANUAL:
                        return Command(remote_enabled=False)
                    return self._drive(c.cruise_kph, c.lane_center_x, v, x, vs, dt)
            if t - self._alert_t >= c.alert_s:
                self.phase, self._v_ref, self._decel_t = DECEL, v, t
                self._log("decel_start", t)
            else:
                cmd = self._drive(self._hold_kph, c.lane_center_x, v, x, vs, dt)
                cmd.hazard = True
                return cmd

        if self.phase == DECEL:
            lane_t = (t - self._decel_t) / LANE_CHANGE_S
            if c.pull_over:
                s = _clamp(lane_t, 0.0, 1.0)
                x_ref = c.lane_center_x + (c.shoulder_center_x - c.lane_center_x) * (s * s * (3 - 2 * s))
                lane_done = lane_t >= 1.0 and abs(x - c.shoulder_center_x) < 0.3 and abs(heading_error_deg(vs)) < 3.0
            else:
                x_ref, lane_done = c.lane_center_x, True
            floor = 0.0 if lane_done else min(self._v_start, PULL_OVER_MIN_KPH)
            self._v_ref = max(floor, self._v_ref - c.decel_kph_per_s * dt)
            cmd = self._drive(self._v_ref, x_ref, v, x, vs, dt)
            cmd.hazard = True
            if self._v_ref <= 0.0 and v < STOP_KPH:
                self._stopped_ticks += 1
                if self._stopped_ticks >= 5:
                    self._on_stopped(t, vs)
            else:
                self._stopped_ticks = 0
            return cmd

        # STOPPED
        return Command(brake=1.0, hazard=True, ecall=True, remote_enabled=True)

    # ---------- 내부 ----------
    def _takeover_condition(self, dms: DmsInfo | None) -> bool:
        if dms is None or dms.level != DANGER:
            return False
        return any(r.startswith(p) for r in dms.danger_reasons for p in self.cfg.takeover_reasons)

    def _drive(self, v_ref, x_ref, v, x, vs, dt, ramp=False) -> Command:
        if ramp:
            v_ref = max(0.0, v - self.cfg.decel_kph_per_s)
        e = v_ref - v
        if v_ref <= 0.0:
            self._i = 0.0
            throttle, brake = 0.0, 1.0 if v < 3.0 else _clamp(KB_SPEED * (v - v_ref) + 0.05, 0.0, 1.0)
        elif e >= -1.0:
            if abs(e) < 5.0:                            # 목표 근처에서만 적분 (가속 중 와인드업 방지)
                self._i = _clamp(self._i + KI_SPEED * e * dt, 0.0, 0.6)
            throttle, brake = _clamp(KP_SPEED * e + self._i, 0.0, 1.0), 0.0
        else:
            self._i = max(0.0, self._i - 0.2 * dt)
            throttle, brake = 0.0, _clamp(KB_SPEED * (-e), 0.0, 1.0)
        h_ref = _clamp(K_LAT_DEG * (x_ref - x), -HEAD_REF_LIMIT, HEAD_REF_LIMIT)
        steer = _clamp(K_HEAD * (h_ref - heading_error_deg(vs)), -STEER_LIMIT, STEER_LIMIT)
        return Command(throttle=throttle, brake=brake, steering=steer, remote_enabled=True)

    def _reset_emergency(self):
        self._danger_since = None
        self._quiet_since = None
        self._alert_t = 0.0
        self._decel_t = 0.0
        self._v_ref = 0.0
        self._v_start = 0.0
        self._hold_kph = 0.0
        self._stopped_ticks = 0
        self._resume_phase = CRUISE

    def _start_emergency(self, t, reasons, dms, vs):
        c = self.cfg
        self._resume_phase = self.phase if self.phase in (CRUISE, MANUAL) else CRUISE
        self.events = []
        self._log("test_trigger" if reasons == ["TEST TRIGGER"] else "detected", t)
        v = float(vs.get("speed_kph", 0.0)) if vs else 0.0
        self._hold_kph = c.cruise_kph if self._resume_phase == CRUISE else v
        self._v_start = max(v, self._hold_kph)
        self.phase, self._alert_t = ALERT, t
        self.message = "driver unresponsive: hazard on"
        # e-Call 패킷(MSD)은 개입이 확정되는 순간 만들어 둔다 (요구: 감지 후 1초 이내 생성).
        # 위치·정차 시각은 정차 후 채워서 저장한다.
        t0 = time.perf_counter()
        self.msd = {
            "msd_id": str(uuid.uuid4()),
            "msd_version": 1,
            "test_call": True,
            "vehicle_id": c.vehicle_id,
            "timestamp_utc": _utc_now(),
            "state": "EMERGENCY",
            "reason": list(reasons),
            "last_hr_bpm": round(dms.hr_bpm, 1) if dms and dms.hr_valid else None,
            "position": None,
            "detected_at": _utc_now(),
            "stopped_at": None,
            "note": "research prototype - not a real emergency call",
        }
        self._log("msd_created", t + (time.perf_counter() - t0))
        print(f"[vehicle] 개입 시작: {', '.join(reasons)} → 비상등")

    def _on_stopped(self, t, vs):
        c = self.cfg
        self.phase = STOPPED
        self._log("stopped", t)
        pos = vs.get("position") or {}
        x, z = float(pos.get("x", 0.0)), float(pos.get("z", 0.0))
        lat = c.virtual_lat + z / 111_320.0
        lon = c.virtual_lon + x / (111_320.0 * math.cos(math.radians(c.virtual_lat)))
        self.msd.update(position={"lat": round(lat, 7), "lon": round(lon, 7), "unity_x": round(x, 2),
                                  "unity_z": round(z, 2), "source": "unity_virtual"},
                        stopped_at=_utc_now(), on_shoulder=x > 3.0)
        self.msd["timeline_ms"] = self.timeline_ms()
        d = Path(c.ecall_dir)
        d.mkdir(parents=True, exist_ok=True)
        self.msd_path = d / f"msd_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{self.msd['msd_id'][:8]}.json"
        self.msd_path.write_text(json.dumps(self.msd, indent=2, ensure_ascii=False), encoding="utf-8")
        self._log("ecall_saved", t)
        self.message = f"e-Call MSD saved: {self.msd_path.name}"
        print(f"[vehicle] 정차 완료 → e-Call MSD 저장: {self.msd_path}  timeline(ms)={self.msd['timeline_ms']}")

    def _log(self, name: str, t: float):
        self.events.append((name, t))

    def timeline_ms(self) -> dict:
        """첫 이벤트(감지) 기준 각 단계까지 걸린 시간(ms)."""
        if not self.events:
            return {}
        t0 = self.events[0][1]
        return {name: round((t - t0) * 1000.0, 1) for name, t in self.events}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------- TCP 서버
class VehicleLink:
    """팀원 vehicle_tcp_server.py 와 같은 규격의 TCP 서버 + VehicleController.

    메인(카메라) 루프는 set_dms() 로 판정 결과만 넘기고, 제어 계산과 송수신은 이 스레드가 20Hz 로 한다.
    카메라 루프가 멈추면(DMS_STALE_S) 명령을 끊어 Unity 페일세이프(자동 제동)가 동작하게 한다.
    """

    def __init__(self, cfg, host: str | None = None, port: int | None = None):
        self.cfg = cfg
        self.host, self.port = host or cfg.unity_host, port or cfg.unity_port
        self.ctrl = VehicleController(cfg)
        self.lock = threading.Lock()
        self.vehicle_state: dict | None = None
        self._state_t = 0.0
        self.dms: DmsInfo | None = None
        self._dms_t = 0.0
        self.command = Command()
        self.client: str | None = None
        self._unity_seq: int | None = None
        self._seq = 0
        self.running = False
        self.thread: threading.Thread | None = None
        self.bound = threading.Event()

    # ---------- 메인 스레드에서 호출 ----------
    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._serve, daemon=True, name="UnityVehicleLink")
        self.thread.start()
        self.bound.wait(2.0)

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(2.0)

    def set_dms(self, s):
        with self.lock:
            self.dms = DmsInfo.from_state(s)
            self._dms_t = time.monotonic()

    def toggle_cruise(self):
        with self.lock:
            self.ctrl.toggle_cruise()

    def resolve(self):
        with self.lock:
            self.ctrl.resolve()

    def trigger_test(self):
        with self.lock:
            self.ctrl.trigger_test(time.monotonic())

    def snapshot(self) -> dict:
        with self.lock:
            vs = self.vehicle_state if time.monotonic() - self._state_t < STATE_STALE_S else None
            return {"client": self.client, "port": self.port, "phase": self.ctrl.phase,
                    "speed_kph": vs.get("speed_kph") if vs else None,
                    "pos": (vs.get("position") or {}) if vs else None,
                    "command": asdict(self.command), "message": self.ctrl.message,
                    "cruise_kph": self.cfg.cruise_kph}

    # ---------- 네트워크 스레드 ----------
    def _tick(self) -> str | None:
        """한 주기 제어 계산 → 보낼 JSON 한 줄 (판정 결과가 끊겼으면 None)."""
        now = time.monotonic()
        with self.lock:
            if now - self._dms_t > DMS_STALE_S:
                return None
            vs = self.vehicle_state if now - self._state_t < STATE_STALE_S else None
            cmd = self.ctrl.step(now, vs, self.dms)
            self.command = cmd
            self._seq += 1
            msg = {"type": "vehicle_control", "sequence": self._seq, **asdict(cmd),
                   # 아래는 팀원 Unity 가 아직 안 쓰는 확장 필드 (JsonUtility 는 모르는 키를 무시함)
                   "phase": self.ctrl.phase, "dms_level": LEVEL_NAME.get(self.dms.level, "?"),
                   "dms_reasons": self.dms.reasons, "msd_id": self.ctrl.msd["msd_id"] if self.ctrl.msd else ""}
        return json.dumps(msg, separators=(",", ":")) + "\n"

    def _on_line(self, line: str):
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            print(f"[vehicle] 잘못된 JSON 무시: {line[:80]}")
            return
        if m.get("type") == "vehicle_state":
            with self.lock:
                seq = m.get("sequence", 0)
                if self._unity_seq is not None and seq < self._unity_seq:      # Unity Play 를 다시 시작함
                    self.ctrl.new_session("Unity restarted")
                self._unity_seq = seq
                self.vehicle_state, self._state_t = m, time.monotonic()

    def _serve(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                srv.bind((self.host, self.port))
            except OSError as e:
                print(f"[vehicle] 포트 {self.port} 를 열 수 없음 ({e}). vehicle_tcp_server.py 가 켜져 있으면 끄세요.")
                self.bound.set()
                return
            srv.listen(1)
            srv.settimeout(0.5)
            self.bound.set()
            print(f"[vehicle] Unity 접속 대기 {self.host}:{self.port}")
            while self.running:
                try:
                    conn, addr = srv.accept()
                except socket.timeout:
                    continue
                try:
                    self._handle(conn, addr)
                except OSError as e:
                    print(f"[vehicle] 연결 오류: {e}")
                finally:
                    conn.close()
                    with self.lock:
                        self.client = None
                    print("[vehicle] Unity 연결 끊김, 재접속 대기")

    def _handle(self, conn: socket.socket, addr):
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.settimeout(0.01)
        with self.lock:
            self.client = f"{addr[0]}:{addr[1]}"
        print(f"[vehicle] Unity 접속: {self.client}")
        period = 1.0 / max(1.0, self.cfg.unity_send_hz)
        next_t = time.monotonic()
        buf = b""
        while self.running:
            try:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
                *lines, buf = buf.split(b"\n")          # 줄바꿈 단위로 자르고 남은 조각은 보관
                for ln in lines:
                    if ln.strip():
                        self._on_line(ln.decode("utf-8", "replace"))
            except socket.timeout:
                pass
            now = time.monotonic()
            if now >= next_t:
                next_t = max(next_t + period, now)
                out = self._tick()
                if out:
                    conn.sendall(out.encode("utf-8"))
