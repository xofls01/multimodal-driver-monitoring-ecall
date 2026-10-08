"""Unity 차량 연동: 제어 로직(가상 차량) + TCP 서버(가짜 Unity 클라이언트).  실행: py -m pytest tests -q"""
import json
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_car import FakeCar
from dms.config import Config
from dms.monitor import OK, WARN, DANGER, State
from dms.vehicle_link import (VehicleController, VehicleLink, DmsInfo, Command,
                              CRUISE, MANUAL, ALERT, DECEL, STOPPED, ROAD_END)

DT = 0.05
OK_DMS = DmsInfo(level=OK)
EYES = DmsInfo(level=DANGER, reasons=["EYES CLOSED 3.0s"], danger_reasons=["EYES CLOSED 3.0s"])


def make(tmp_path, **kw):
    cfg = Config(ecall_dir=str(tmp_path / "ecall"), **kw)
    return cfg, VehicleController(cfg), FakeCar()


def run(ctrl, car, dms_fn, dur, t0=0.0):
    t, cmd = t0, None
    for _ in range(int(round(dur / DT))):
        cmd = ctrl.step(t, car.state(), dms_fn(t))
        car.apply(cmd, DT)
        t += DT
    return t, cmd


def test_cruise_holds_speed_and_lane(tmp_path):
    cfg, ctrl, car = make(tmp_path)
    car.x = 0.2                                              # 차로 중앙에서 벗어난 채 출발
    t, cmd = run(ctrl, car, lambda t: OK_DMS, 10.0)
    assert ctrl.phase == CRUISE and cmd.remote_enabled and not cmd.hazard
    assert abs(car.v * 3.6 - cfg.cruise_kph) < 1.0 and abs(car.x - cfg.lane_center_x) < 0.1


def test_eyes_closed_emergency_stops_on_shoulder_and_saves_msd(tmp_path):
    cfg, ctrl, car = make(tmp_path)
    t, _ = run(ctrl, car, lambda t: OK_DMS, 8.0)
    t, cmd = run(ctrl, car, lambda t: EYES, 1.9, t)
    assert ctrl.phase == CRUISE                              # takeover_after_s(2초) 전
    t, cmd = run(ctrl, car, lambda t: EYES, 0.2, t)
    assert ctrl.phase == ALERT and cmd.hazard and abs(car.v * 3.6 - cfg.cruise_kph) < 1.5
    t, cmd = run(ctrl, car, lambda t: EYES, 25.0, t)
    assert ctrl.phase == STOPPED and cmd.ecall and cmd.hazard and cmd.brake == 1.0
    assert car.v < 0.2 and abs(car.x - cfg.shoulder_center_x) < 0.5     # 갓길 위
    files = list((tmp_path / "ecall").glob("msd_*.json"))
    assert len(files) == 1
    msd = json.loads(files[0].read_text(encoding="utf-8"))
    assert msd["test_call"] and msd["vehicle_id"] == cfg.vehicle_id and msd["reason"] == ["EYES CLOSED 3.0s"]
    assert msd["position"]["source"] == "unity_virtual" and msd["on_shoulder"] and msd["stopped_at"]
    tl = msd["timeline_ms"]
    assert tl["msd_created"] - tl["detected"] < 1000                    # 요구사항: 감지 후 1초 이내 패킷 생성
    assert tl["decel_start"] == cfg.alert_s * 1000 and tl["stopped"] > tl["decel_start"]


def test_emergency_is_latched_after_decel(tmp_path):
    cfg, ctrl, car = make(tmp_path)
    t, _ = run(ctrl, car, lambda t: OK_DMS, 8.0)
    t, _ = run(ctrl, car, lambda t: EYES, 4.5, t)               # 2초 개입 + 2초 경고 → 감속 시작
    assert ctrl.phase == DECEL
    t, cmd = run(ctrl, car, lambda t: OK_DMS, 25.0, t)          # 감속이 시작된 뒤 깨어나도 끝까지 정차
    assert ctrl.phase == STOPPED and cmd.ecall
    ctrl.resolve()                                              # [G] 해제
    t, cmd = run(ctrl, car, lambda t: OK_DMS, 10.0, t)
    assert ctrl.phase == CRUISE and not cmd.hazard and not cmd.ecall and car.v * 3.6 > 25


def test_alert_cancelled_when_driver_wakes(tmp_path):
    cfg, ctrl, car = make(tmp_path)
    t, _ = run(ctrl, car, lambda t: OK_DMS, 8.0)
    t, _ = run(ctrl, car, lambda t: EYES, 2.5, t)
    assert ctrl.phase == ALERT
    t, cmd = run(ctrl, car, lambda t: OK_DMS, 1.2, t)           # 경고 중 1초 이상 정상 → 취소
    assert ctrl.phase == CRUISE and not cmd.hazard and ctrl.message == "driver responded"
    assert not (tmp_path / "ecall").exists()


def test_non_consciousness_danger_does_not_take_over(tmp_path):
    cfg, ctrl, car = make(tmp_path)
    perclos = DmsInfo(level=DANGER, reasons=["PERCLOS 35%", "EYES CLOSED 1.3s"], danger_reasons=["PERCLOS 35%"])
    hands = DmsInfo(level=DANGER, reasons=["HANDS OFF WHEEL 6.0s"], danger_reasons=["HANDS OFF WHEEL 6.0s"])
    warn = DmsInfo(level=WARN, reasons=["EYES CLOSED 1.5s"], danger_reasons=[])
    for d in (perclos, hands, warn):
        t, cmd = run(ctrl, car, lambda t: d, 10.0)
        assert ctrl.phase == CRUISE and not cmd.hazard


def test_manual_mode_and_takeover_from_manual(tmp_path):
    cfg, ctrl, car = make(tmp_path)
    ctrl.toggle_cruise()                                        # [A] → 수동
    assert ctrl.phase == MANUAL
    t, cmd = run(ctrl, car, lambda t: OK_DMS, 1.0)
    assert cmd == Command(remote_enabled=False)                 # Unity 키보드 운전
    car.v = 40 / 3.6                                            # 사람이 40km/h 로 몰고 있다고 가정
    t, cmd = run(ctrl, car, lambda t: EYES, 2.2, t)
    assert ctrl.phase == ALERT and cmd.remote_enabled and cmd.hazard
    t, cmd = run(ctrl, car, lambda t: OK_DMS, 1.5, t)           # 깨어나면 원래 모드(수동)로
    assert ctrl.phase == MANUAL and cmd == Command(remote_enabled=False)


def test_test_trigger_and_road_end(tmp_path):
    cfg, ctrl, car = make(tmp_path)
    t, _ = run(ctrl, car, lambda t: OK_DMS, 3.0)
    ctrl.trigger_test(t)                                        # [T] 시험용 개입: 정상이어도 취소되지 않음
    t, cmd = run(ctrl, car, lambda t: OK_DMS, 25.0, t)
    assert ctrl.phase == STOPPED and json.loads(ctrl.msd_path.read_text(encoding="utf-8"))["reason"] == ["TEST TRIGGER"]

    cfg, ctrl, car = make(tmp_path)
    car.z = cfg.road_end_z - 5
    t, cmd = run(ctrl, car, lambda t: OK_DMS, 8.0)
    assert ctrl.phase == ROAD_END and car.v < 0.2 and not cmd.ecall
    car.z = -5.0                                                # Unity 에서 R (출발점 복귀)
    t, cmd = run(ctrl, car, lambda t: OK_DMS, 0.1, t)
    assert ctrl.phase == CRUISE


def test_monitor_danger_reasons():
    from dms.monitor import DriverMonitor
    from dms.perception import FaceObs
    import numpy as np
    m = DriverMonitor(Config(calib_seconds=1.0, perclos_window_s=30))
    face = lambda ear: FaceObs(pts=np.zeros((478, 2), np.float32), ear_l=ear, ear_r=ear, ear=ear, mar=.1,
                               pitch=0, yaw=0, roll=0, nose_ratio=.5)
    t = 0.0
    for _ in range(45):
        m.update(t, face(.3), [], None, (640, 480)); t += 1 / 30
    for _ in range(90):                                         # 3초 감김 → EYES CLOSED 가 DANGER
        s = m.update(t, face(.1), [], None, (640, 480)); t += 1 / 30
    assert s.level == DANGER and any(r.startswith("EYES CLOSED") for r in s.danger_reasons)


# ---------------- TCP ----------------
def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def read_lines(sock, n, timeout=2.0):
    sock.settimeout(timeout)
    buf, out = b"", []
    end = time.monotonic() + timeout
    while len(out) < n and time.monotonic() < end:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        buf += chunk
        *lines, buf = buf.split(b"\n")
        out += [json.loads(l) for l in lines if l.strip()]
    return out


def test_tcp_link_with_fake_unity(tmp_path):
    cfg = Config(ecall_dir=str(tmp_path / "ecall"))
    link = VehicleLink(cfg, host="127.0.0.1", port=free_port())
    link.start()
    try:
        u = socket.create_connection(("127.0.0.1", link.port), timeout=2)
        car = FakeCar()
        u.sendall((json.dumps(car.state()) + "\n").encode())
        assert read_lines(u, 1, timeout=0.4) == []             # 판정 결과가 없으면 명령을 안 보냄
        st = State(level=OK)
        for _ in range(10):                                     # 카메라 루프가 판정을 넘겨주는 동안
            link.set_dms(st)
            u.sendall((json.dumps(car.state()) + "\n").encode())
            time.sleep(0.05)
        msgs = read_lines(u, 5)
        assert len(msgs) >= 5
        m = msgs[-1]
        assert m["type"] == "vehicle_control" and m["remote_enabled"] and m["throttle"] > 0
        assert {"brake", "steering", "hazard", "ecall", "sequence", "phase", "dms_level"} <= set(m)
        assert m["phase"] == CRUISE and m["dms_level"] == "OK"
        assert [x["sequence"] for x in msgs] == sorted(x["sequence"] for x in msgs)
        snap = link.snapshot()
        assert snap["client"] and snap["speed_kph"] is not None
        time.sleep(0.7)                                         # 카메라 루프가 멈추면 명령도 멈춤 → Unity 페일세이프
        read_lines(u, 100, timeout=0.1)
        assert read_lines(u, 1, timeout=0.4) == []
        u.close()
    finally:
        link.stop()


def test_vehicle_hud_renders_all_phases():
    import numpy as np
    from dms.ui import draw_vehicle
    from dataclasses import asdict
    base = {"client": "127.0.0.1:5555", "port": 5001, "speed_kph": 29.8, "pos": {"x": 0.75, "z": 120.0},
            "command": asdict(Command(throttle=.1, remote_enabled=True)), "message": "", "cruise_kph": 30.0}
    for phase in (CRUISE, MANUAL, ALERT, DECEL, STOPPED, ROAD_END):
        img = np.zeros((720, 1280, 3), np.uint8)
        draw_vehicle(img, {**base, "phase": phase, "message": "msg" if phase == STOPPED else ""})
        assert img.any()
    img = np.zeros((720, 1280, 3), np.uint8)
    draw_vehicle(img, {**base, "client": None, "phase": CRUISE, "speed_kph": None, "pos": None})
    draw_vehicle(img, None)
    assert img.any()


def test_new_session_after_stop(tmp_path):
    cfg, ctrl, car = make(tmp_path)
    t, _ = run(ctrl, car, lambda t: OK_DMS, 5.0)
    ctrl.trigger_test(t)
    t, cmd = run(ctrl, car, lambda t: OK_DMS, 20.0, t)
    assert ctrl.phase == STOPPED and cmd.brake == 1.0
    car.x, car.z, car.h = 0.75, -5.0, 0.0                       # Unity 에서 Play 재시작 / R → 출발점
    t, cmd = run(ctrl, car, lambda t: OK_DMS, 5.0, t)
    assert ctrl.phase == CRUISE and not cmd.hazard and car.v * 3.6 > 20


def test_link_detects_unity_restart(tmp_path):
    link = VehicleLink(Config(ecall_dir=str(tmp_path / "e")))
    link.ctrl.phase = STOPPED
    link._on_line(json.dumps({"type": "vehicle_state", "sequence": 500, "position": {"z": 100}}))
    assert link.ctrl.phase == STOPPED
    link._on_line(json.dumps({"type": "vehicle_state", "sequence": 0, "position": {"z": -5}}))
    assert link.ctrl.phase == CRUISE
