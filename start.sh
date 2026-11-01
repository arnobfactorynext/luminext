#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
exec .venv/bin/python -m luminext --host "${LUMINEXT_HOST:-127.0.0.1}" --port "${LUMINEXT_PORT:-8000}"
