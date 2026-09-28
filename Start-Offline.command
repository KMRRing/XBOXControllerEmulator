#!/bin/bash
# Double-click this (macOS / Linux). No GitHub: the newest basis-vN.zip in Downloads is the update.
cd "$(dirname "$0")" || exit 1
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "Python 3 is needed once: https://python.org/downloads"
  read -r -p "Press return to close."
  exit 1
fi
"$PY" launcher.py --source zip "$@" || read -r -p "Press return to close."
