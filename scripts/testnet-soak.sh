#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
export SOAK_UID=$(id -u) SOAK_GID=$(id -g)
mkdir -p data/testnet-soak
dc() { docker compose -p automoney-soak -f compose.testnet-soak.yaml "$@"; }
case "${1:-status}" in
 start) dc up --build -d ;;
 status) dc run --rm --no-deps testnet-soak status ;;
 stop) dc run --rm --no-deps testnet-soak stop ;;
 logs) dc logs --tail 30 ;;
 *) echo '用法：bash scripts/testnet-soak.sh {start|status|stop|logs}'; exit 1 ;;
esac
