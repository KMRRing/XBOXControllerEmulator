#!/bin/bash
# Double-click this to cut a version. A window opens; nothing is typed at a prompt.
cd "$(dirname "$0")" || exit 1
PY="$(command -v python3 || command -v python)"
[ -z "$PY" ] && { echo "Python 3 is needed once: https://python.org/downloads"; read -r -p "Press return."; exit 1; }
"$PY" publish.py "$@" || read -r -p "Press return to close."
