"""통합본 차량 서버(Unity/python/vehicle_tcp_server.py)를 e-Call 과 함께 실행 (PC).

    py run_vehicle_with_ecall.py --ecall-broker 192.168.0.64 --model-port 5002

통합본 파일은 한 글자도 수정하지 않는다. 이 스크립트가 vehicle_tcp_server 를 그대로 불러온 뒤,
실행 중인 메모리에서만 두 곳에 알림을 덧붙인다.
  - SharedData.set_state  : Unity vehicle_state 를 받을 때 → Stopped 감지 → Pi 로 e-Call 요청
  - extract_model_state   : 모델(5002) 패킷을 읽을 때 → Danger 근거(사유·심박) 기억
두 알림 모두 원래 함수를 먼저(또는 그대로) 실행하고 결과를 바꾸지 않으며, 오류가 나도 차량 제어에 영향을 주지 않는다.
--ecall-* 외의 옵션은 vehicle_tcp_server.py 에 그대로 넘어간다.

저장된 모델 결과 + CAN 시나리오 재생(Unity/python/play_can_model.py)으로 시연할 때:

    py run_vehicle_with_ecall.py --ecall-broker 192.168.0.64 --ecall-entry play_can danger --recording <결과폴더>
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VEHICLE_DIR = HERE.parent / "Unity" / "python"
for p in (str(HERE), str(VEHICLE_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from ecall_link import EcallLink  # noqa: E402

# 차량 서버를 띄우는 통합본 진입점 (모두 vehicle_tcp_server.SharedData 를 씀)
ENTRIES = {
    "vehicle": "vehicle_tcp_server",   # 실시간 모델(5002) 또는 콘솔 state danger
    "play_can": "play_can_model",      # 저장된 모델 결과 + CAN 시나리오 재생
}


def install_hooks(link: EcallLink, extra_modules=()):
    """vehicle_tcp_server / model_state_client 에 알림을 붙이고, 원래대로 되돌리는 함수를 돌려준다.

    extra_modules: `from model_state_client import extract_model_state` 로 이름을 따로 가져간 모듈
    (예: play_can_model). 그 모듈의 이름도 같은 알림 함수로 바꾼다.
    """
    import model_state_client as msc
    import vehicle_tcp_server as vts

    original_set_state = vts.SharedData.set_state
    original_extract = msc.extract_model_state

    def set_state(self, state):
        original_set_state(self, state)
        try:
            link.on_vehicle_state(state)
        except Exception as exception:
            print(f"[ECALL] 요청 처리 오류(차량 제어에는 영향 없음): {exception}")

    def extract_model_state(packet, max_age_s=0.75):
        try:
            if isinstance(packet, dict):
                link.on_model_packet(packet)
        except Exception as exception:
            print(f"[ECALL] 모델 근거 기록 오류(판정에는 영향 없음): {exception}")
        return original_extract(packet, max_age_s)

    patched = [m for m in extra_modules if getattr(m, "extract_model_state", None) is original_extract]
    vts.SharedData.set_state = set_state
    msc.extract_model_state = extract_model_state
    for module in patched:
        module.extract_model_state = extract_model_state

    def restore():
        vts.SharedData.set_state = original_set_state
        msc.extract_model_state = original_extract
        for module in patched:
            module.extract_model_state = original_extract
    return restore


def parse_ecall_args(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 add_help=False)
    ap.add_argument("--ecall-broker", required=True, help="라즈베리파이(Mosquitto) IP")
    ap.add_argument("--ecall-port", type=int, default=1883)
    ap.add_argument("--ecall-config", type=Path, default=HERE / "config.json",
                    help="vehicle_id 를 읽을 설정. Pi 의 config.json 과 같은 vehicle_id 여야 함")
    ap.add_argument("--ecall-lat", type=float, default=35.8468, help="Unity 원점의 가상 위도")
    ap.add_argument("--ecall-lon", type=float, default=127.1294, help="Unity 원점의 가상 경도")
    ap.add_argument("--ecall-log", type=Path, default=HERE / "ecall_requests.jsonl")
    ap.add_argument("--ecall-entry", choices=ENTRIES, default="vehicle",
                    help="vehicle: vehicle_tcp_server.py, play_can: play_can_model.py")
    return ap.parse_known_args(argv)


def main():
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print(__doc__)
        print("e-Call 옵션: --ecall-broker IP (필수), --ecall-port, --ecall-config, --ecall-lat, --ecall-lon, "
              "--ecall-log, --ecall-entry {vehicle,play_can}")
        return
    args, rest = parse_ecall_args(sys.argv[1:])
    entry = importlib.import_module(ENTRIES[args.ecall_entry])
    vehicle_id = json.loads(args.ecall_config.read_text(encoding="utf-8"))["vehicle_id"]
    link = EcallLink(broker=args.ecall_broker, port=args.ecall_port, vehicle_id=vehicle_id,
                     base_lat=args.ecall_lat, base_lon=args.ecall_lon, log_path=args.ecall_log)
    link.start()
    print(f"[ECALL] 정차(Stopped) 시 {link.trigger_topic} → {args.ecall_broker}:{args.ecall_port}  "
          f"기록: {args.ecall_log}")
    restore = install_hooks(link, extra_modules=[entry])
    sys.argv = [str(entry.__file__), *rest]
    try:
        entry.main()
    finally:
        restore()
        link.stop()


if __name__ == "__main__":
    main()
