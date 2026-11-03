#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
exec cloudflared tunnel --url "http://localhost:${PORT:-8000}"