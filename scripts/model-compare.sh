#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
if [[ $# -ne 1 && $# -ne 3 ]]; then
  echo '用法：bash scripts/model-compare.sh 匯出.zip [--run-cloud 商業模型名稱]'; exit 1
fi
if [[ $# -eq 3 && "$2" != --run-cloud ]]; then exit 1; fi
export_path="$(cd -- "$(dirname -- "$1")" && pwd)/$(basename -- "$1")"
[[ -f "$export_path" ]] || { echo '找不到 ZIP'; exit 1; }
mkdir -p artifacts/model-comparison
output_root="$(pwd)/artifacts/model-comparison"
run_name="run-$(date +%Y%m%d-%H%M%S)"
extra=()
if [[ $# -eq 3 ]]; then extra=(--run-cloud --cloud-model "$3"); fi
docker build -t automoney-model-compare .
docker run --rm --network host --user "$(id -u):$(id -g)" \
  --env-file config/openteddy.env \
  -v "$export_path:/input/export.zip:ro" -v "$output_root:/results" \
  --entrypoint python automoney-model-compare -m trader.model_compare \
  /input/export.zip --output "/results/$run_name" --limit 6 "${extra[@]}"
echo "結果：$output_root/$run_name"
