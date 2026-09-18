#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
if [ ! -f config/ollama.env ]; then
  echo '請先設定 config/ollama.env 的 Ollama URL 與模型。'
  exit 1
fi
dc() { docker compose -p automoney-altcoins --env-file config/ollama.env -f compose.yaml -f compose.ai.yaml -f compose.altcoins.yaml "$@"; }
case "${1:-help}" in
  start) dc up --build -d trader ai-trader dashboard ;;
  stop) dc stop ai-trader trader dashboard ;;
  status) dc ps ;;
  logs) dc logs --tail 40 ai-trader ;;
  pause) dc run --rm trader kill ;;
  resume) dc run --rm trader resume ;;
  *) echo '用法：bash scripts/altcoins.sh {start|stop|status|logs|pause|resume}；獨立 SOL/XRP 模擬，頁面 8082。' ;;
esac
