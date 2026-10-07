#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
mkdir -p data/testnet-acceptance
extra=()
if [[ -f config/testnet.env ]]; then extra=(--env-file config/testnet.env); fi
docker build -t automoney-testnet-acceptance .
docker run --rm --user "$(id -u):$(id -g)" "${extra[@]}" \
  -v "$(pwd)/data/testnet-acceptance:/acceptance" \
  --entrypoint python automoney-testnet-acceptance -m trader.testnet_acceptance \
  --data /acceptance "$@"
