#!/usr/bin/env sh
# Thin wrapper — the real script is scripts/repro.py (single source of truth).
# Candidates are validated by actually running them: on Windows, a "python"
# on PATH can be the Microsoft Store alias stub, which only prints an error.
for PY in python3 python py; do
    if command -v "$PY" >/dev/null 2>&1 && "$PY" -c "import sys" >/dev/null 2>&1; then
        exec "$PY" "$(dirname "$0")/repro.py" "$@"
    fi
done
echo "No working Python found (tried: python3, python, py)" >&2
exit 1
