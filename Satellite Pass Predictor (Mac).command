#!/bin/bash
# macOS launcher — double-click to run
cd "$(dirname "$0")"

if ! command -v python3 &>/dev/null; then
    echo "Python 3 is required. Install from https://www.python.org/downloads/"
    echo "Press any key to exit..."
    read -n 1
    exit 1
fi

python3 satpp.py
