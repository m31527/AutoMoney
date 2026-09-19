#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
export EXIT_PAIR="${2:-btc-eth}"
case "$EXIT_PAIR" in
  btc-eth) export EXIT_PORT=8083 ;;
  sol-xrp) export EXIT_PORT=8084 ;;
  *) echo '幣組只能是 btc-eth 或 sol-xrp'; exit 1 ;;
esac
dc() { docker compose -p "automoney-exit-v2-$EXIT_PAIR" -f compose.yaml -f compose.exit-v2.yaml "$@"; }
case "${1:-help}" in
  start) dc up --build -d trader dashboard ;;
  stop) dc stop trader dashboard ;;
  status) dc ps ;;
  logs) dc logs --tail 30 trader ;;
  pause) dc run --rm trader kill ;;
  resume) dc run --rm trader resume ;;
  *) echo '用法：bash scripts/research-v2.sh {start|stop|status|logs|pause|resume} {btc-eth|sol-xrp}' ;;
esac
