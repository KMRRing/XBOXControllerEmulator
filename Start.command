#!/bin/bash
# The online Mac/Linux machine needs this file and nothing else. First run asks for your GitHub token,
# fetches launcher.py from the repository, and runs it. It also works unchanged inside an unpacked zip.
cd "$(dirname "$0")" || exit 1

# --- identity: publish.py writes this block from app/appinfo.json ---
APP_NAME="XBOXControllerEmulator"
APP_REPO="KMRRing/XBOXControllerEmulator"
# --- end identity ---

HOME_DIR="${HOME}/${APP_NAME}"
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "  Python 3 is needed once: https://python.org/downloads"
  read -r -p "  Press return to close."
  exit 1
fi

if [ ! -f launcher.py ]; then
  echo
  echo "  ${APP_NAME} lives in the private repository ${APP_REPO}."
  echo "  Paste your GitHub token - fine-grained, Contents: read and write."
  echo
  read -r -p "  Token: " TOKEN
  if [ -z "$TOKEN" ]; then
    echo "  No token, so there is nothing to fetch."
    read -r -p "  Press return to close."
    exit 1
  fi
  if ! curl -fsSL -H "Authorization: Bearer ${TOKEN}" -H "Accept: application/vnd.github.raw" \
       "https://api.github.com/repos/${APP_REPO}/contents/launcher.py" -o launcher.py; then
    rm -f launcher.py
    echo "  Could not fetch the launcher: check the token, its access to ${APP_REPO}, and the network."
    read -r -p "  Press return to close."
    exit 1
  fi
  mkdir -p "$HOME_DIR" && printf '%s\n' "$TOKEN" > "${HOME_DIR}/token.txt"
fi

"$PY" launcher.py "$@" || read -r -p "  Press return to close."
