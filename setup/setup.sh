#!/bin/bash
# Setup wizard bootstrap for Boondockers' Helper.
# Usage: bash setup/setup.sh [--skip-launchd | --skip-config]
set -euo pipefail

# Run from src/ regardless of where the script is invoked.
cd "$(dirname "$0")/.."

# 1. Python version gate (requires 3.10+)
if ! python3 -c "import sys; assert sys.version_info >= (3, 10)" 2>/dev/null; then
    echo "ERROR: Python 3.10+ required." >&2
    echo "       Install via https://python.org or: brew install python@3.13" >&2
    echo "       Current version: $(python3 --version 2>&1 || echo 'not found')" >&2
    exit 1
fi

# 2. Dependency gate — victron-ble must be importable.
if ! python3 -c "import victron_ble" 2>/dev/null; then
    echo "Python dependencies are not installed."
    read -r -p "Install now via 'pip install -r requirements.txt'? [Y/n] " ans
    ans="${ans:-Y}"
    if [[ "${ans^^}" == "Y" ]]; then
        python3 -m pip install -r requirements.txt
    else
        echo ""
        echo "Run:  pip install -r requirements.txt" >&2
        echo "Then: bash setup/setup.sh" >&2
        exit 1
    fi
fi

exec python3 setup/wizard.py "$@"
