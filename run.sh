#!/bin/bash

# Get the folder directory where this bash script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Navigate to that directory
cd "$SCRIPT_DIR"

export QT_IM_MODULE=fcitx
export XMODIFIERS=@im=fcitx

# Activate the virtual environment (looks for 'venv' or '.venv')
if [ -d "venv" ]; then
    source venv/bin/activate
elif [ -d ".venv" ]; then
    source .venv/bin/activate
else
    echo "Error: Virtual environment (venv or .venv) not found in $SCRIPT_DIR"
    exit 1
fi

# Run the Python script
python main.py

# Optional: keep the environment active or just exit
deactivate

