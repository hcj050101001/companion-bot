#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
uv sync
[ -f .env ] || cp .env.example .env
[ -f config.yaml ] || cp config.example.yaml config.yaml
printf '%s\n' 'Edit .env and config.yaml before starting the application.'
