#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
case "${1:-btc-eth}" in
  btc-eth) project=automoney; extra=() ;;
  sol-xrp) project=automoney-altcoins; extra=(-f compose.altcoins.yaml) ;;
  *) echo '用法：bash scripts/ai-shadow-pair.sh {btc-eth|sol-xrp}'; exit 1 ;;
esac
docker compose -p "$project" --env-file config/ollama.env \
  -f compose.yaml -f compose.ai.yaml "${extra[@]}" -f compose.ai-shadow-pair.yaml \
  up --build -d --no-deps ai-trader dashboard
