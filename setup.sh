#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required but was not found. Install Docker Desktop or Docker Engine first." >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose v2 is required. Verify that 'docker compose version' works." >&2
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example. Edit it before starting the agent."
else
  echo ".env already exists; leaving it unchanged."
fi

mkdir -p artifacts

echo "Building the Playwright service image..."
docker compose build

echo
echo "Setup complete. Start the service with:"
echo "  ./runner.sh"
echo "  docker compose up"
echo
echo "Configure AWS_REGION, BEDROCK_MODEL_ID, and AWS credentials in .env."
