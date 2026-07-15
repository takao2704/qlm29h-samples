#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/frontend"
pnpm install --frozen-lockfile
pnpm build

echo "Dashboard assets are ready in $SCRIPT_DIR/backend/static"
