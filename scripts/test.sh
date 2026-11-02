#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
exec uv run pytest -q --timeout=10
