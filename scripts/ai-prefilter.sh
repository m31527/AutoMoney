#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
if [ ! -f config/ollama.env ]; then
  echo '請先設定 config/ollama.env 的 Ollama URL 與模型。'
  exit 1
fi
case "${2:-btc-eth}" in
  btc-eth) project=automoney; extra=() ;;
  sol-xrp) project=automoney-altcoins; extra=(-f compose.altcoins.yaml) ;;
  *) echo '幣組只能是 btc-eth 或 sol-xrp'; exit 1 ;;
esac
dc() { docker compose -p "$project" --env-file config/ollama.env -f compose.yaml -f compose.ai.yaml "${extra[@]}" -f compose.ai-prefilter.yaml "$@"; }
export AI_PREFILTER_ENABLED=true
case "${1:-help}" in
  start) dc up --build -d --no-deps ai-trader dashboard ;;
  disable) export AI_PREFILTER_ENABLED=false; dc up --build -d --no-deps ai-trader dashboard ;;
  status) dc ps ;;
  logs) dc logs --tail 40 ai-trader ;;
  *) echo '用法：bash scripts/ai-prefilter.sh {start|disable|status|logs} {btc-eth|sol-xrp}' ;;
esac
