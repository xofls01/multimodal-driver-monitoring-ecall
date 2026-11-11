#!/bin/sh
set -eu
driver_project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -x "$driver_project_dir/.venv/bin/python" ]; then
    driver_python="$driver_project_dir/.venv/bin/python"
elif [ -x "$driver_project_dir/../driver-state-monitoring-system/.venv/bin/python" ]; then
    driver_python="$driver_project_dir/../driver-state-monitoring-system/.venv/bin/python"
else
    echo "Python environment missing. Follow README.md to create this copy's .venv." >&2
    exit 1
fi
cd "$driver_project_dir"
driver_explicit_mode=false
for driver_arg in "$@"; do
    case "$driver_arg" in
        --config|--rules|--config=*|--rules=*) driver_explicit_mode=true ;;
    esac
done
if [ "$driver_explicit_mode" = false ]; then
    exec "$driver_python" scripts/run_vision_baseline.py \
        --config config/prototype/vision_baseline.json --rules config/prototype/decision_rules.json "$@"
fi
exec "$driver_python" scripts/run_vision_baseline.py "$@"
