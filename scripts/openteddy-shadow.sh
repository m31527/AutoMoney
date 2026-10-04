#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
case "${1:-btc-eth}" in
  btc-eth) project=automoney; extra=() ;;
  sol-xrp) project=automoney-altcoins; extra=(-f compose.altcoins.yaml) ;;
  *) echo '用法：bash scripts/openteddy-shadow.sh {btc-eth|sol-xrp}'; exit 1 ;;
esac
compose=(docker compose -p "$project" --env-file config/ollama.env
  -f compose.yaml -f compose.ai.yaml "${extra[@]}" -f compose.ai-shadow-pair.yaml
  -f compose.openteddy.yaml)
"${compose[@]}" build ai-trader
# Verify from Docker, without invoking any model or paying for tokens.
"${compose[@]}" run --rm --no-deps --entrypoint python ai-trader -c '
import json, os, urllib.request
r = urllib.request.Request(os.environ["OPENTEDDY_URL"].rstrip("/") + "/automoney/health",
    headers={"Authorization": "Bearer " + os.environ["OPENTEDDY_TOKEN"]})
from trader.exchange.transport import NoRedirect
opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))
with opener.open(r, timeout=10) as response:
    health = json.loads(response.read(4096))
    if health.get("version", 0) < 2:
        raise SystemExit("請先安裝新版 OpenTeddy bridge 並重啟 OpenTeddy")
    print(health)
'
"${compose[@]}" up -d --no-deps ai-trader dashboard
