#!/bin/bash
# Start pptagent DeepPresenter WebUI
# Usage: ./start.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Bypass proxy for localhost (avoids 502 Bad Gateway on startup check)
NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1 \
  .venv/bin/python webui.py
