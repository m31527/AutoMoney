#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
# Exporting fixes the same model for download and worker, including if .env differs.
export OLLAMA_MODEL="${OLLAMA_MODEL:-qwen3.8:27b}"
dc() { docker compose -f compose.yaml -f compose.ollama.yaml -f compose.nvidia.yaml "$@"; }
case "${1:-help}" in
  setup)
    docker info >/dev/null
    nvidia-smi
    dc build trader dashboard ai-trader
    dc up -d --wait ollama
    dc exec -T ollama ollama pull "$OLLAMA_MODEL"
    # Load once before collecting trade context: model loading can be slow.
    dc exec -T ollama ollama run "$OLLAMA_MODEL" "Reply with OK only."
    dc up -d trader dashboard ai-trader
    echo '已啟動。請在 Spark 瀏覽器開啟 http://localhost:8080'
    ;;
  start) dc up -d trader dashboard ai-trader ;;
  stop) dc stop ai-trader trader dashboard ollama ;;
  logs) dc logs --tail 40 ai-trader ;;
  status) dc ps ;;
  gpu) dc exec -T ollama ollama ps ;;
  pause) dc run --rm trader kill ;;
  resume) dc run --rm trader resume ;;
  *) echo '用法：bash scripts/spark.sh {setup|start|stop|logs|status|gpu|pause|resume}' ;;
esac
