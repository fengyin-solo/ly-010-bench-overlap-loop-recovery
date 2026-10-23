#!/bin/bash
set -euo pipefail

SOLUTION_DIR="$(cd "$(dirname "$0")" && pwd)"
cp "$SOLUTION_DIR/reference.py" /app/loop_recovery.py
chmod 0755 /app/loop_recovery.py

cd /app
python3 loop_recovery.py data/session.json data/recovery.json
python3 loop_recovery.py data/session.json data/recovery.json
