#!/usr/bin/env bash
# Pull the configured Ollama model into the compose-managed daemon.
# Runs on the HOST (the ollama CLI lives inside the container, not on the host).
# Model selection is entirely environment-driven via OLLAMA_MODEL in paladin/.env.
# Usage: ./scripts/setup_ollama.sh (from anywhere; defaults to the local model)
set -euo pipefail
cd "$(dirname "$0")/.."
set -a
# shellcheck disable=SC1091
[ -f .env ] && . ./.env
set +a
MODEL="${OLLAMA_MODEL:-llama3.2:3b}"
# -T disables pseudo-TTY allocation so the pull works in non-interactive shells/CI.
docker compose exec -T ollama ollama pull "$MODEL"
