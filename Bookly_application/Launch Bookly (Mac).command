#!/bin/bash
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "First run: setting up (takes ~30s)..."
  python3 -m venv .venv || { echo "Python 3 is required: https://www.python.org/downloads/"; read -p "Press Enter to close"; exit 1; }
fi
source .venv/bin/activate
pip install -q -r requirements.txt
python app.py
