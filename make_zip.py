"""make_zip — build the release zip. One artifact, three transports.

The same bytes are uploaded to the GitHub release, downloaded by the laptop launcher, and dropped in the
offline laptop's Downloads folder. Inside, the app's files sit at the top with `launcher.py` and the
double-click files beside them, so unpacking it anywhere gives a folder that runs.

    python make_zip.py [OUT.zip]

`data/` and `repo-data/` are never in it: the code is disposable, the data is not.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(ROOT, "app")
TOP = ("launcher.py", "Start.bat", "Start-Offline.bat", "Start.command", "Start-Offline.command", "README.md")
SKIP_DIR = {"__pycache__", ".git", ".venv", "data", "repo-data", ".github", ".pytest_cache"}
SKIP_FILE = {".DS_Store", ".gitignore", "token.txt"}
EXECUTABLE = (".command", ".sh")


def info() -> dict:
    with open(os.path.join(APP, "appinfo.json"), encoding="utf-8") as f:
        return json.load(f)


def version() -> str:
    with open(os.path.join(APP, "VERSION"), encoding="utf-8") as f:
        return f.read().strip()


def members() -> list:
    """(absolute path, name inside the zip) — the app at the top, the launcher beside it."""
    out = []
    for base, dirs, names in os.walk(APP):
        dirs[:] = [d for d in dirs if d not in SKIP_DIR]
        for n in sorted(names):
            if n in SKIP_FILE or n.endswith((".pyc", ".tmp")):
                continue
            full = os.path.join(base, n)
            out.append((full, os.path.relpath(full, APP).replace("\\", "/")))
    for n in TOP:
        full = os.path.join(ROOT, n)
        if os.path.isfile(full):
            out.append((full, n))
    return out


def build(out_path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    files = members()
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for full, name in files:
            item = zipfile.ZipInfo.from_file(full, name)
            item.compress_type = zipfile.ZIP_DEFLATED
            mode = 0o755 if name.endswith(EXECUTABLE) else 0o644
            item.external_attr = (stat.S_IFREG | mode) << 16   # the .command files stay double-clickable
            with open(full, "rb") as f:
                z.writestr(item, f.read())
    return out_path


def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    app = info()
    out = argv[0] if argv else os.path.join(ROOT, f"{app['slug']}-{version()}.zip")
    build(out)
    print(f"{out}  ({os.path.getsize(out) / 1e6:.2f} MB, {len(members())} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
