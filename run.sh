#!/bin/bash
#
# Start SrvWatch in the foreground (manual run / testing).
# For a persistent service, use: ./deploy/install-launchd.sh install
#
set -e
cd "$(dirname "$0")"

PY="${PYTHON:-/usr/bin/python3}"
exec "$PY" srvwatch.py
