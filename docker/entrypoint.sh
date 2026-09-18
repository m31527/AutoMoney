#!/bin/sh
set -eu

if [ "${1:-}" = "ollama-start" ]; then
    shift
    exec python -m trader --config /app/config/default.toml ollama-run \
        --cycles "${PAPER_CYCLES:-0}" "$@"
fi

if [ "${1:-}" = "compare-start" ]; then
    shift
    exec python -m trader --config /app/config/default.toml compare-run \
        --cycles "${PAPER_CYCLES:-0}" "$@"
fi

if [ "${1:-}" = "paper-start" ]; then
    shift
    # Initialization is idempotent: an existing balance is never reset.
    python -m trader --config /app/config/default.toml paper-init
    exec python -m trader --config /app/config/default.toml paper-run \
        --strategy "${PAPER_STRATEGY:-sma}" \
        --symbol "${PAPER_SYMBOL:-BTCUSDT}" --cycles "${PAPER_CYCLES:-0}" "$@"
fi

exec python -m trader --config /app/config/default.toml "$@"
