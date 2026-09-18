#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
if [ ! -f config/ollama.env ]; then
  cp config/ollama.env.example config/ollama.env
  echo '已建立 config/ollama.env，請先設定 OLLAMA_BASE_URL 與 OLLAMA_MODEL，再執行本指令。'
  exit 1
fi
dc() { docker compose --env-file config/ollama.env -f compose.yaml -f compose.ai.yaml "$@"; }
case "${1:-help}" in
  start) dc up --build -d trader ai-trader dashboard ;;
  stop) dc stop ai-trader trader dashboard ;;
  status) dc ps ;;
  logs) dc logs --tail 40 ai-trader ;;
  pause) dc run --rm trader kill ;;
  resume) dc run --rm trader resume ;;
  *) echo '用法：bash scripts/ai.sh {start|stop|status|logs|pause|resume}；在網頁按「匯出分析資料」下載 ZIP。' ;;
esac
