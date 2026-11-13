#!/bin/sh
set -eu
driver_project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -x "$driver_project_dir/.venv/bin/python" ]; then
    driver_python="$driver_project_dir/.venv/bin/python"
elif [ -x "$driver_project_dir/../driver-state-monitoring-system/.venv/bin/python" ]; then
    driver_python="$driver_project_dir/../driver-state-monitoring-system/.venv/bin/python"
else
    echo "Create .venv and install requirements.txt first." >&2
    exit 1
fi
cd "$driver_project_dir"
if [ "${1:-}" = "--unity-replay" ]; then
    shift
    exec "$driver_python" scripts/replay_multimodal_unity.py --recording "$@"
fi
if [ "${1:-}" = "--cached-preview" ]; then
    shift
    exec "$driver_python" scripts/run_multimodal_replay.py "$@"
fi
exec "$driver_python" scripts/run_multimodal_system.py "$@"
