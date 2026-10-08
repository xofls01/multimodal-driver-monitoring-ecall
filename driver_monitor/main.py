"""운전자 상태 모니터링 (눈 감김 / 졸음·머리 숙임 / 핸들 파지)

사용 예:
    py main.py                      # 기본 웹캠(0)
    py main.py --source 1           # 다른 카메라
    py main.py --source test.mp4    # 녹화 영상으로 테스트
    py main.py --config my.json --log run.csv
    py main.py --unity                 # Unity 차량 연동 (정속 주행 + 비상 정차 + e-Call)
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import threading
import time
from pathlib import Path

# Windows MSMF 의 하드웨어 변환을 끄면 카메라 여는 시간이 10초대 → 1초 미만으로 줄어든다
# (켜져 있으면 열 때 멈춘 것처럼 보임). 카메라를 열기 전에 설정돼 있어야 한다.
os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")

import cv2

from dms.config import Config
from dms.monitor import DriverMonitor, DANGER, WARN, LEVEL_NAME
from dms.perception import Perception
from dms.rppg import RPPG
from dms.ui import draw_face, draw_hands, draw_hud, draw_rppg, draw_vehicle, draw_wheel
from dms.vehicle_link import VehicleLink
from dms.wheel import WheelROI

WIN = "Driver Monitor"


# ---------------- 경고음 ----------------
class Beeper:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self._last = 0.0

    def __call__(self, level: int):
        if not self.enabled or level < WARN:        # OK / CAUTION 은 소리 없음
            return
        gap = 0.6 if level == DANGER else 2.0
        now = time.time()
        if now - self._last < gap:
            return
        self._last = now
        threading.Thread(target=self._beep, args=(level,), daemon=True).start()

    @staticmethod
    def _beep(level):
        try:
            if sys.platform.startswith("win"):
                import winsound
                winsound.Beep(1800 if level == DANGER else 1000, 250)
            else:
                print("\a", end="", flush=True)
        except Exception:
            pass


# ---------------- ROI 그리기 (마우스 드래그) ----------------
class RoiDrawer:
    def __init__(self):
        self.mode: str | None = None   # "ellipse" | "rect" | None
        self.p0 = self.p1 = None
        self.result: WheelROI | None = None
        self.frame_wh = (1, 1)

    def start(self, shape):
        self.mode, self.p0, self.p1, self.result = shape, None, None, None

    def on_mouse(self, event, x, y, flags, param):
        if self.mode is None:
            return
        if event == cv2.EVENT_LBUTTONDOWN:
            self.p0 = self.p1 = (x, y)
        elif event == cv2.EVENT_MOUSEMOVE and self.p0 is not None and (flags & cv2.EVENT_FLAG_LBUTTON):
            self.p1 = (x, y)
        elif event == cv2.EVENT_LBUTTONUP and self.p0 is not None:
            self.p1 = (x, y)
            w, h = self.frame_wh
            if abs(self.p1[0] - self.p0[0]) > 10 and abs(self.p1[1] - self.p0[1]) > 10:
                self.result = WheelROI.from_drag(self.p0, self.p1, w, h, self.mode)
            self.mode = None

    def draw(self, img):
        if self.mode is None:
            return
        cv2.putText(img, f"Drag to draw {self.mode.upper()} ROI around the wheel (ESC cancel)",
                    (10, img.shape[0] // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
        if self.p0 and self.p1:
            if self.mode == "rect":
                cv2.rectangle(img, self.p0, self.p1, (0, 255, 255), 2)
            else:
                c = ((self.p0[0] + self.p1[0]) // 2, (self.p0[1] + self.p1[1]) // 2)
                ax = (abs(self.p1[0] - self.p0[0]) // 2, abs(self.p1[1] - self.p0[1]) // 2)
                cv2.ellipse(img, c, ax, 0, 0, 360, (0, 255, 255), 2)


def _set_size(cap, width, height):
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FPS, 30)
    return cap


def open_camera(index: int, width: int, height: int, dshow: bool = False) -> cv2.VideoCapture:
    """Windows: MSMF(하드웨어 변환 끔)로 열고, 안 되면 DirectShow.

    이 PC 웹캠 실측 (1280x720): MSMF 30fps / DirectShow 는 YUY2 로 잡혀 10fps,
    MJPG 를 열 때 지정해도 15fps. DirectShow 는 드라이버 설정창(--cam-settings)이 필요할 때만 쓴다.
    """
    if not sys.platform.startswith("win"):
        return _set_size(cv2.VideoCapture(index), width, height)
    if not dshow:
        cap = _set_size(cv2.VideoCapture(index, cv2.CAP_MSMF), width, height)
        if cap.isOpened() and cap.read()[0]:
            return cap
        cap.release()
        print("[camera] MSMF 로 열기 실패 → DirectShow 로 시도 (720p 에서는 FPS 가 낮을 수 있음)")
    # DirectShow 는 MJPG 를 열 때 넘겨야 적용된다 (열고 나서 set 하면 무시되고 YUY2 로 남음)
    mjpg = cv2.VideoWriter_fourcc(*"MJPG")
    return cv2.VideoCapture(index, cv2.CAP_DSHOW, [
        cv2.CAP_PROP_FOURCC, mjpg, cv2.CAP_PROP_FRAME_WIDTH, width,
        cv2.CAP_PROP_FRAME_HEIGHT, height, cv2.CAP_PROP_FPS, 30])


def parse_args():
    ap = argparse.ArgumentParser(description="Driver monitoring: eyes / head / hands")
    ap.add_argument("--source", default="0", help="카메라 번호 또는 영상 파일 경로")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--config", default="config.json", help="설정 파일 (없으면 기본값)")
    ap.add_argument("--dump-config", action="store_true", help="기본 설정을 config.json 으로 저장 후 종료")
    ap.add_argument("--no-hands", action="store_true", help="손 검출 끄기 (속도 향상)")
    ap.add_argument("--no-rppg", action="store_true", help="심박(rPPG) 추정 끄기")
    ap.add_argument("--cam-settings", action="store_true",
                    help="(Windows) 카메라 드라이버 설정창 열기: 자동 노출/화이트밸런스를 끄면 rPPG 가 좋아짐")
    ap.add_argument("--lock-camera", action="store_true",
                    help="자동 노출/화이트밸런스 끄기 시도 (카메라/드라이버마다 지원 여부 다름)")
    ap.add_argument("--mirror", action="store_true", help="좌우 반전")
    ap.add_argument("--log", default=None, help="CSV 로그 경로")
    ap.add_argument("--no-beep", action="store_true")
    ap.add_argument("--unity", action="store_true",
                    help="Unity 차량 연동: TCP 서버(기본 5001)를 열고 정속 주행, 의식 저하 시 비상 정차 + e-Call")
    ap.add_argument("--unity-port", type=int, default=None, help="Unity TCP 포트 (기본: config 의 unity_port)")
    return ap.parse_args()


def main():
    args = parse_args()
    cfg = Config.load(args.config)
    if args.dump_config:
        cfg.save(args.config)
        print(f"저장됨: {args.config}")
        return
    if args.no_hands:
        cfg.enable_hands = False
    if args.no_beep:
        cfg.beep = False
    if args.no_rppg:
        cfg.enable_rppg = False

    is_file = not args.source.isdigit()
    if is_file:
        cap = cv2.VideoCapture(args.source)
    else:
        # 드라이버 설정창은 DirectShow 에서만 열림
        cap = open_camera(int(args.source), args.width, args.height, dshow=args.cam_settings)
        if cap.isOpened():
            print(f"[camera] {cap.getBackendName()} {int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x"
                  f"{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}")
        if args.lock_camera:
            cap.set(cv2.CAP_PROP_AUTO_WB, 0)
            cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)   # V4L2/MSMF 에서 '수동' 값
            print(f"[camera] auto_wb={cap.get(cv2.CAP_PROP_AUTO_WB)} "
                  f"auto_exposure={cap.get(cv2.CAP_PROP_AUTO_EXPOSURE)} (지원 안 하면 값이 그대로)")
        if args.cam_settings:
            cap.set(cv2.CAP_PROP_SETTINGS, 1)
    if not cap.isOpened():
        sys.exit(f"카메라/영상을 열 수 없음: {args.source}")

    perc = Perception(enable_hands=cfg.enable_hands)
    mon = DriverMonitor(cfg)
    rppg = RPPG(cfg) if cfg.enable_rppg else None
    wheel = WheelROI.load(cfg.wheel_file)
    if wheel:
        print(f"[wheel] {cfg.wheel_file} 로드됨")
    beep = Beeper(cfg.beep)
    link = VehicleLink(cfg, port=args.unity_port) if args.unity else None
    if link:
        link.start()
    drawer = RoiDrawer()
    mirror, show_help = args.mirror, True

    cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(WIN, drawer.on_mouse)

    log_f = log_w = None
    if args.log:
        log_f = open(args.log, "w", newline="", encoding="utf-8")
        log_w = csv.writer(log_f)
        log_w.writerow(["t", "level", "level_name", "reasons", "ear", "ear_thr", "eyes_closed", "eyes_closed_s", "perclos",
                        "pitch_delta", "head_down", "head_down_s", "nods", "mar", "yawns",
                        "hands_detected", "hands_on_wheel", "hands_off_s", "one_hand_s",
                        "hr", "hr_snr", "hr_valid", "hr_base", "rmssd", "rppg_roi", "vehicle_phase", "vehicle_kph"])

    t0 = time.perf_counter()
    fps, last = 0.0, time.perf_counter()
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if mirror:
                frame = cv2.flip(frame, 1)
            h, w = frame.shape[:2]
            drawer.frame_wh = (w, h)

            t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0 if is_file else time.perf_counter() - t0
            face, hands = perc.process(frame, int(t * 1000))
            r = rppg.update(t, frame, face) if rppg else None
            s = mon.update(t, face, hands, wheel, (w, h), rppg=r)
            beep(s.level)
            if link:
                link.set_dms(s)
            vsnap = link.snapshot() if link else None

            now = time.perf_counter()
            fps = 0.9 * fps + 0.1 * (1.0 / max(now - last, 1e-6))
            last = now

            vis = frame
            draw_wheel(vis, wheel, cfg.wheel_ring_inner, cfg.wheel_margin)
            draw_face(vis, face)
            draw_hands(vis, hands)
            draw_rppg(vis, r)
            draw_hud(vis, s, fps, show_help, cfg)
            draw_vehicle(vis, vsnap)
            drawer.draw(vis)
            cv2.imshow(WIN, vis)

            if drawer.result is not None:
                wheel = drawer.result
                wheel.save(cfg.wheel_file)
                drawer.result = None
                print(f"[wheel] 저장: {cfg.wheel_file} ({wheel.shape})")

            if log_w and not s.calibrating:
                # level: 0=OK 1=CAUTION 2=WARNING 3=DANGER
                log_w.writerow([f"{t:.3f}", s.level, LEVEL_NAME[s.level], "|".join(s.reasons), f"{s.ear:.4f}", f"{s.ear_thr:.4f}",
                                int(s.eyes_closed), f"{s.eyes_closed_s:.2f}", f"{s.perclos:.3f}",
                                f"{s.pitch_delta:.2f}", int(s.head_down), f"{s.head_down_s:.2f}", s.nods,
                                f"{s.mar:.3f}", s.yawns, s.hands_detected, s.hands_on_wheel,
                                f"{s.hands_off_s:.2f}", f"{s.one_hand_s:.2f}", f"{s.hr:.1f}",
                                f"{s.hr_snr:.2f}", int(s.hr_valid), f"{s.hr_base:.1f}", f"{s.rmssd:.1f}",
                                s.hr_roi, vsnap["phase"] if vsnap else "",
                                f"{vsnap['speed_kph']:.1f}" if vsnap and vsnap["speed_kph"] is not None else ""])

            k = cv2.waitKey(1) & 0xFF
            if k in (ord("q"), ord("Q")):
                break
            elif k == 27:
                drawer.mode = None
            elif k in (ord("c"), ord("C")):
                mon.reset_calibration()
                if rppg:
                    rppg.reset("recalibrating")
                print("[calib] 재캘리브레이션 시작")
            elif k in (ord("w"), ord("W")):
                drawer.start("ellipse")
            elif k in (ord("r"), ord("R")):
                drawer.start("rect")
            elif k in (ord("x"), ord("X")):
                wheel = None
                Path(cfg.wheel_file).unlink(missing_ok=True)
            elif k in (ord("h"), ord("H")):
                show_help = not show_help
            elif k in (ord("m"), ord("M")):
                mirror = not mirror
            elif link and k in (ord("a"), ord("A")):
                link.toggle_cruise()
            elif link and k in (ord("g"), ord("G")):
                link.resolve()
            elif link and k in (ord("t"), ord("T")):
                link.trigger_test()
            elif k in (ord("s"), ord("S")):
                fn = f"snap_{int(time.time())}.jpg"
                cv2.imwrite(fn, vis)
                print(f"[snap] {fn}")
    finally:
        cap.release()
        perc.close()
        if link:
            link.stop()
        cv2.destroyAllWindows()
        if log_f:
            log_f.close()


if __name__ == "__main__":
    main()
