#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
export LIVE_UID=$(id -u) LIVE_GID=$(id -g)
dc() { docker compose -p automoney -f compose.live-auto.yaml --profile auto-live "$@"; }
case "${1:-status}" in
 build) dc build live-auto ;;
 status) dc run --rm --no-deps live-auto status ;;
 arm)
  read -r -p '請輸入「我確認啟用80USDT自動實盤」：' confirmation
  dc run --rm --no-deps live-auto arm --confirmation "$confirmation"
  ;;
 start) dc up -d --no-deps live-auto ;;
 stop)
  # The running worker checks the same persistent switch before any POST.
  dc run --rm --no-deps --entrypoint python live-auto -c 'from pathlib import Path; from trader.live_control import operate; operate(Path("/live"),Path("/soak"),"stop",{})'
  dc stop live-auto
  dc run --rm --no-deps live-auto disarm
  ;;
 logs) dc logs --tail 30 live-auto ;;
 *) echo '用法：bash scripts/live-auto.sh {build|status|arm|start|stop|logs}'; exit 1 ;;
esac
