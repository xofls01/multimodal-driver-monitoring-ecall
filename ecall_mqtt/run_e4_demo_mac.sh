#!/usr/bin/env bash
# macOS 한 번에 실행: Unity 에디터 + e-Call 서비스 + e-Call 수신 화면 + 차량 서버(E4)
#
#   (저장소 루트에서) bash ecall_mqtt/run_e4_demo_mac.sh          # 저장된 E4 분석 결과 + CAN danger 재생
#   (저장소 루트에서) bash ecall_mqtt/run_e4_demo_mac.sh --live   # 모델이 E4 영상·생체신호를 지금 분석 (실시간)
#   경로는 이 파일 위치(ecall_mqtt) 기준으로 찾으므로 어느 폴더에서 실행해도 된다.
#
# 실행 후 Unity 에서 BasicDrivingStage1 씬을 열고 Play 를 누른다. 갓길 정차 후 e-Call 이 오면
# 브라우저에 e-Call 수신 화면(http://localhost:8765)이 열린다. Ctrl+C 로 모두 종료.
# 이 Mac 에 맞게 바꿀 값은 환경 변수로 준다. 예:
#   UNITY_PROJECT="$HOME/My project" E4_RECORDING="$HOME/e4_results/E4" bash ecall_mqtt/run_e4_demo_mac.sh
set -euo pipefail

LIVE=0
[ "${1:-}" = "--live" ] && LIVE=1
TAG="[DEMO]"; [ "$LIVE" = 1 ] && TAG="[LIVE]"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"   # 이 스크립트(ecall_mqtt)의 한 단계 위 = 저장소 루트
MODEL_ROOT="$ROOT/driver-state-monitoring-system"
UNITY_PROJECT="${UNITY_PROJECT:-$HOME/My project}"
E4_RECORDING="${E4_RECORDING:-}"
E4_PAIRS="${E4_PAIRS:-}"
BROKER="${BROKER:-localhost}"

# --- Python: 반드시 .venv 파이썬 ---------------------------------------------------------
# play_can_model.py 는 .venv 가 있으면 그 파이썬으로 자기 자신을 다시 실행(execve)한다.
# 그러면 run_vehicle_with_ecall.py 가 붙인 e-Call 알림이 사라지므로, 처음부터 .venv 파이썬으로 실행한다.
PY="$MODEL_ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
    PY="$(command -v python3)"
    echo "$TAG .venv 가 없어 $PY 사용"
fi
if ! "$PY" -c "import paho.mqtt, cv2" 2>/dev/null; then
    echo "$TAG $PY 에 paho-mqtt 또는 opencv 가 없습니다. 먼저 설치하세요:"
    echo "       \"$PY\" -m pip install paho-mqtt opencv-python"
    exit 1
fi

# --- 입력 데이터 ----------------------------------------------------------------------
if [ "$LIVE" = 1 ]; then
    if [ -z "$E4_PAIRS" ]; then
        if [ -f "$MODEL_ROOT/data/local_e4/pairs.json" ]; then E4_PAIRS="data/local_e4/pairs.json"
        else E4_PAIRS="config/multimodal/system_pairs.json"; fi
    fi
    if [ ! -f "$MODEL_ROOT/$E4_PAIRS" ] && [ ! -f "$E4_PAIRS" ]; then
        echo "$TAG 입력 목록이 없습니다: $E4_PAIRS (E4_PAIRS=... 로 지정)"
        exit 1
    fi
else
    if [ -z "$E4_RECORDING" ]; then
        for d in outputs/local_e4_test/E4 outputs/e4_overlay_crop/E4; do
            if [ -f "$MODEL_ROOT/$d/unity_packets.jsonl" ]; then E4_RECORDING="$MODEL_ROOT/$d"; break; fi
        done
    fi
    if [ -z "$E4_RECORDING" ] || [ ! -f "$E4_RECORDING/unity_packets.jsonl" ] || [ ! -f "$E4_RECORDING/preview.mp4" ]; then
        echo "$TAG 저장된 E4 분석 결과가 없습니다: ${E4_RECORDING:-(찾지 못함)}"
        echo "       preview.mp4, unity_packets.jsonl 이 있는 폴더를 E4_RECORDING=... 으로 지정하거나 --live 로 실행하세요."
        exit 1
    fi
fi

# --- MQTT 브로커(Mosquitto) 확인 ------------------------------------------------------
if ! nc -z "$BROKER" 1883 2>/dev/null; then
    echo "$TAG $BROKER:1883 에 MQTT 브로커가 없습니다."
    echo "       Homebrew: brew install mosquitto && brew services start mosquitto"
    exit 1
fi

# --- 1) 이전 실행에서 남은 서버 정리 (MQTT 접속 ID·포트 5001/5002/8765 가 겹치지 않게) ----------
for pid in $(pgrep -f 'ecall_service\.py|run_vehicle_with_ecall\.py|ecall_screen\.py|run_multimodal_system\.py' || true); do
    echo "$TAG 남아 있던 서버 종료: PID $pid"
    kill "$pid" 2>/dev/null || true
done

# --- 2) Unity 에디터: 이 프로젝트가 이미 열려 있으면 그대로 쓰고, 아니면 연다 ---------------
if pgrep -if "Unity.*-projectpath.*$(basename "$UNITY_PROJECT")" >/dev/null; then
    echo "$TAG Unity 이미 실행 중"
else
    UNITY_APP="${UNITY_APP:-}"
    if [ -z "$UNITY_APP" ]; then
        # 프로젝트가 쓰는 버전 → 없으면 설치된 가장 최신 버전
        want="$(sed -n 's/^m_EditorVersion: //p' "$UNITY_PROJECT/ProjectSettings/ProjectVersion.txt" 2>/dev/null || true)"
        if [ -n "$want" ] && [ -d "/Applications/Unity/Hub/Editor/$want/Unity.app" ]; then
            UNITY_APP="/Applications/Unity/Hub/Editor/$want/Unity.app"
        else
            UNITY_APP="$(ls -d /Applications/Unity/Hub/Editor/*/Unity.app 2>/dev/null | sort -V | tail -1 || true)"
        fi
    fi
    if [ -z "$UNITY_APP" ] || [ ! -d "$UNITY_APP" ]; then
        echo "$TAG Unity 에디터를 찾지 못했습니다. UNITY_APP=/Applications/Unity/Hub/Editor/<버전>/Unity.app 로 지정하거나 Unity Hub 에서 직접 여세요."
    else
        echo "$TAG Unity 에디터 실행 중... ($UNITY_APP, 창이 뜨기까지 1~2분)"
        open -na "$UNITY_APP" --args -projectPath "$UNITY_PROJECT"
    fi
fi

# --- 3) 모델(--live) + e-Call 서비스(Pi 역할) + e-Call 수신 화면(폰 역할) ---------------------
PIDS=()
cleanup() {
    for pid in "${PIDS[@]:-}"; do [ -n "$pid" ] && kill "$pid" 2>/dev/null || true; done
}
trap cleanup EXIT INT TERM
export PYTHONDONTWRITEBYTECODE=1

if [ "$LIVE" = 1 ]; then
    # 모델은 차량 서버가 접속할 때(Unity Play 뒤)까지 분석을 멈추고 기다린다.
    # --show: 분석 영상·ECG/PPG 창 / --pace: 영상 시간보다 빨리 처리하지 않음 (느린 Mac 에서는 더 늦어질 수 있음)
    (cd "$MODEL_ROOT" && exec "$PY" -B scripts/run_multimodal_system.py --pairs "$E4_PAIRS" --only E4 \
        --unity --unity-port 5002 --show --pace --output outputs/local_e4_unity) &
    PIDS+=($!)
fi

if [ "$BROKER" = "localhost" ] || [ "$BROKER" = "127.0.0.1" ]; then
    (cd "$ROOT/ecall_mqtt" && exec "$PY" -B ecall_service.py) &
    PIDS+=($!)
else
    echo "$TAG 브로커 $BROKER 의 Pi ecall-service 를 사용합니다 (이 Mac 에서 ecall_service.py 를 띄우지 않음)."
fi

# 터미널 출력 대신 브라우저 화면으로 MSD 를 보여 준다 (e-Call 이 올 때 브라우저가 열림, 화면에서 R = 초기화)
(cd "$ROOT/ecall_mqtt" && exec "$PY" -B ecall_screen.py --broker "$BROKER" --open-on-ecall) &
PIDS+=($!)

# --- 4) 차량 서버 (이 터미널): Unity(5001) ↔ 모델(5002) 또는 저장된 결과, 정차하면 e-Call 요청 -----
echo "$TAG Unity 에서 BasicDrivingStage1 씬을 열고 Play 를 누르세요."
if [ "$LIVE" = 1 ]; then
    "$PY" -B "$ROOT/ecall_mqtt/run_vehicle_with_ecall.py" --ecall-broker "$BROKER" --model-port 5002
else
    "$PY" -B "$ROOT/ecall_mqtt/run_vehicle_with_ecall.py" --ecall-broker "$BROKER" \
        --ecall-entry play_can danger --recording "$E4_RECORDING"
fi
