#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [ ! -f .env ]; then
  echo "Missing .env. Run ./setup.sh first."
  exit 1
fi

mkdir -p artifacts
mkdir -p browser-profile agent-files
exec docker compose up --build "$@"
