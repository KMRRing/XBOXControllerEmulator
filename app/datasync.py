"""datasync — where the app's data lives, and how it travels between devices.

The data folder is `APP_DATA_DIR` when a launcher sets it, otherwise `data/` beside the code. Nothing else in
the app needs to know where that is: `datasync.DATA` is the answer.

Travelling is `repo-data/` inside the repository: one file per data file, plus a manifest recording when each
was last written and by which device. Pull applies a remote file when it is newer than the local one; push
commits every local file whose contents differ from the manifest. Newest-wins per file, not a merge — one
owner at a time.

    python datasync.py pull      apply what is newer on GitHub
    python datasync.py push      commit what changed here
    python datasync.py status    what would move, in either direction

Token and repository come from GH_TOKEN and GH_REPO (every launcher sets both), or from `<data>/token.txt`
and `appinfo.json`.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.environ.get("APP_DATA_DIR") or os.path.join(ROOT, "data"))
REPO_DIR = "repo-data"                      # where the data lives inside the repository
API = os.environ.get("GH_API") or "https://api.github.com"
SKIP_SUFFIX = (".tmp", ".lock", ".log")     # never travels
SKIP_NAME = {"token.txt"}
TIMEOUT = 60

os.makedirs(DATA, exist_ok=True)


def device() -> str:
    return os.environ.get("DEVICE_NAME") or platform.node() or "device"


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def token() -> str:
    if os.environ.get("GH_TOKEN"):
        return os.environ["GH_TOKEN"]
    for p in (os.path.join(DATA, "token.txt"), os.path.join(os.path.dirname(DATA), "token.txt")):
        try:
            with open(p, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            continue
    return ""


def repo() -> str:
    if os.environ.get("GH_REPO"):
        return os.environ["GH_REPO"]
    try:
        with open(os.path.join(ROOT, "appinfo.json"), encoding="utf-8") as f:
            return json.load(f).get("repo", "")
    except (OSError, ValueError):
        return ""


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def travels(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return not (name.startswith(".") or name in SKIP_NAME or name.endswith(SKIP_SUFFIX))


def local_files() -> dict:
    """Every file under the data folder that travels: relative path -> bytes."""
    out = {}
    for base, _dirs, names in os.walk(DATA):
        for n in names:
            p = os.path.join(base, n)
            rel = os.path.relpath(p, DATA).replace("\\", "/")
            if not travels(rel):
                continue
            try:
                with open(p, "rb") as f:
                    out[rel] = f.read()
            except OSError:
                continue
    return out


# --------------------------------------------------------------------------------- this device's ledger
def state_path() -> str:
    return os.path.join(DATA, ".datasync.json")


def state() -> dict:
    try:
        with open(state_path(), encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    st.setdefault("files", {})
    return st


def save_state(st: dict) -> None:
    tmp = state_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, state_path())


# --------------------------------------------------------------------------------- GitHub
def gh(method: str, path: str, tok: str, body: dict | None = None):
    url = path if path.startswith("http") else API + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "datasync",
                                          "Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {tok}"} if tok else {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r) if r.headers.get_content_type() == "application/json" else r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def fetch(tok: str, rp: str, path: str) -> bytes | None:
    doc = gh("GET", f"/repos/{rp}/contents/{REPO_DIR}/{path}", tok)
    if not isinstance(doc, dict) or "content" not in doc:
        return None
    return base64.b64decode(doc["content"])


def put(tok: str, rp: str, path: str, data: bytes, message: str) -> None:
    existing = gh("GET", f"/repos/{rp}/contents/{REPO_DIR}/{path}", tok)
    body = {"message": message, "content": base64.b64encode(data).decode("ascii")}
    if isinstance(existing, dict) and existing.get("sha"):
        body["sha"] = existing["sha"]
    gh("PUT", f"/repos/{rp}/contents/{REPO_DIR}/{path}", tok, body)


def manifest(tok: str, rp: str) -> dict:
    raw = fetch(tok, rp, "manifest.json")
    if not raw:
        return {"version": 1, "files": {}}
    try:
        man = json.loads(raw.decode("utf-8"))
    except ValueError:
        return {"version": 1, "files": {}}
    man.setdefault("files", {})
    return man


def reachable(tok: str = "", rp: str = "") -> bool:
    tok, rp = tok or token(), rp or repo()
    if not rp:
        return False
    try:
        return gh("GET", f"/repos/{rp}", tok) is not None
    except Exception:                                          # noqa: BLE001
        return False


# --------------------------------------------------------------------------------- pull / push / status
def pull(tok: str = "", rp: str = "") -> dict:
    tok, rp = tok or token(), rp or repo()
    if not rp:
        return {"error": "no repository", "applied": [], "kept_local": [], "unchanged": 0}
    man, st = manifest(tok, rp), state()
    applied, kept, unchanged = [], [], 0
    for path, meta in man["files"].items():
        if not travels(path):
            continue
        local = os.path.join(DATA, path)
        have = None
        if os.path.isfile(local):
            with open(local, "rb") as f:
                have = f.read()
        if have is not None and sha(have) == meta.get("sha256"):
            unchanged += 1
            continue
        mine = (st["files"].get(path) or {}).get("updated_at", "")
        if have is not None and mine and mine > (meta.get("updated_at") or ""):
            kept.append(path)                                  # written here later: it wins, and push will say so
            continue
        data = fetch(tok, rp, path)
        if data is None:
            continue
        os.makedirs(os.path.dirname(local) or DATA, exist_ok=True)
        with open(local, "wb") as f:
            f.write(data)
        st["files"][path] = {"sha256": sha(data), "updated_at": meta.get("updated_at") or now()}
        applied.append(path)
    save_state(st)
    return {"applied": applied, "kept_local": kept, "unchanged": unchanged}


def push(tok: str = "", rp: str = "", message: str = "") -> dict:
    tok, rp = tok or token(), rp or repo()
    if not rp:
        return {"error": "no repository", "pushed": [], "unchanged": 0}
    man, st = manifest(tok, rp), state()
    pushed, skipped = [], 0
    for path, data in local_files().items():
        digest = sha(data)
        known = man["files"].get(path) or {}
        if known.get("sha256") == digest:
            st["files"][path] = {"sha256": digest, "updated_at": known.get("updated_at") or now()}
            skipped += 1
            continue
        put(tok, rp, path, data, message or f"{device()}: {path}")
        stamp = now()
        man["files"][path] = {"sha256": digest, "updated_at": stamp, "device": device(), "bytes": len(data)}
        st["files"][path] = {"sha256": digest, "updated_at": stamp}
        pushed.append(path)
    if pushed:
        man.update(version=1, generated=now(), device=device())
        put(tok, rp, "manifest.json", json.dumps(man, indent=1).encode(), f"{device()}: manifest")
    save_state(st)
    return {"pushed": pushed, "unchanged": skipped}


def status(tok: str = "", rp: str = "") -> dict:
    tok, rp = tok or token(), rp or repo()
    man = manifest(tok, rp) if (rp and tok) else {"files": {}}
    local = local_files()
    ahead = [p for p, d in local.items() if (man["files"].get(p) or {}).get("sha256") != sha(d)]
    behind = [p for p, m in man["files"].items() if p not in local or sha(local[p]) != m.get("sha256")]
    return {"data": DATA, "repo": rp, "linked": bool(rp and tok), "local_files": len(local),
            "to_push": sorted(ahead), "to_pull": sorted(behind)}


def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    what = argv[0] if argv else "status"
    if what == "pull":
        r = pull()
        print(r.get("error") or f"data pull: {len(r['applied'])} applied, "
                                f"{len(r['kept_local'])} kept local, {r['unchanged']} unchanged")
    elif what == "push":
        r = push()
        print(r.get("error") or (f"data push: {len(r['pushed'])} files"
                                 + (f" - {', '.join(r['pushed'][:6])}" if r["pushed"] else " (nothing changed)")))
    else:
        print(json.dumps(status(), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
