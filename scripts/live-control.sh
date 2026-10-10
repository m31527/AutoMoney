#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
export LIVE_UID=$(id -u) LIVE_GID=$(id -g)
case "${1:-status}" in
 setup)
  mkdir -p data/live
  if [ ! -e config/live.env ]; then
   umask 077
   python3 - <<'PY'
from pathlib import Path
import secrets
p = Path('config/live.env')
p.write_text(Path('config/live.env.example').read_text().replace('LIVE_CONTROL_TOKEN=', 'LIVE_CONTROL_TOKEN=' + secrets.token_urlsafe(48)))
p.chmod(0o600)
PY
  fi
  echo 'config/live.env 已準備；填入正式金鑰。ENABLE_LIVE_TRADING 預設 false。'
  ;;
 start)
  test -f config/live.env
  mkdir -p data/live
  docker compose -p automoney --env-file config/ollama.env -f compose.yaml -f compose.ai.yaml -f compose.ai-shadow-pair.yaml -f compose.openteddy.yaml -f compose.live.yaml up --build -d --no-deps live-control dashboard
  ;;
 status) docker compose -p automoney -f compose.yaml -f compose.live.yaml ps live-control dashboard ;;
 *) echo '用法：bash scripts/live-control.sh {setup|start|status}'; exit 1 ;;
esac
