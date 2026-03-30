#!/bin/bash
# Linux launcher
cd "$(dirname "$0")"

if ! command -v python3 &>/dev/null; then
    echo "Python 3 is required. Install with: sudo apt install python3 python3-pip"
    echo "Press any key to exit..."
    read -n 1
    exit 1
fi

python3 satpp.py
