#!/usr/bin/env python3
"""launcher — the one file a device needs, whichever of the three ways it is fed.

Five steps, in this order, in a console that stays open:

    1. find the newest code    a GitHub release, or the newest zip in Downloads, or the folder this sits in
    2. install it              into versions/<tag>; that code carries this file, so the launcher replaces
                               itself when the copy inside is newer, and starts again
    3. pull the data           repo-data/ from GitHub into the data folder, newest-wins per file
    4. run the app             a browser on the app's port; the launcher waits here
    5. push the data           when the window closes, every changed data file goes back to GitHub

The transport is the only thing that differs between devices:

    Start                    laptop with GitHub          --source github
    Start Offline            laptop without GitHub       --source zip     newest <slug>-vN.zip in Downloads
    Pythonista / Shortcuts   the phone                    detected; the app runs inside this process

    --check          what is installed and what is available, then stop
    --offline        do not touch GitHub at all
    --no-push        do not push when the app closes
    --pin v0.4       stay on a version   ·   --pin off   ·   --rollback   go back one
    --force          reinstall the newest version even if it is already here
    --reset          forget the repository and the token
    --home DIR       somewhere other than %LOCALAPPDATA%\\<Name>
    --downloads DIR  where to look for zips
    --smoke          run everything, stop as soon as the app answers (the self-test)

The token needs Contents: read and write, nothing else, and lives in <home>/token.txt.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile

LAUNCHER_VERSION = "2.0.0"

# --- identity: publish.py writes this block from app/appinfo.json --------------------------
APP_NAME = "XBOXControllerEmulator"
APP_SLUG = "xboxcontrolleremulator"
DEFAULT_REPO = "KMRRing/XBOXControllerEmulator"
DEFAULT_PORT = 5033
ENTRY = "app.py"
# --- end identity ------------------------------------------------------------------------------------------

API = os.environ.get("GH_API") or "https://api.github.com"
HTTP_TIMEOUT = 90
def on_ios() -> bool:
    """Pythonista and Pyto both run Python inside their own process, so the app has to run there too rather
    than as a subprocess. Python reports `sys.platform == "ios"` only from 3.13 onwards; older embeddings
    (Pyto ships 3.10) still say darwin, so look for the marker modules those apps provide as well."""
    if sys.platform == "ios" or os.environ.get("LAUNCHER_FORCE_IOS"):
        return True
    for marker in ("objc_util", "pyto"):                       # Pythonista, Pyto
        try:
            if importlib.util.find_spec(marker) is not None:
                return True
        except (ImportError, ValueError):
            continue
    return False


IOS = on_ios()
ICONS = ("Start.bat", "Start-Offline.bat", "Start.command", "Start-Offline.command")
KEEP_OUT = ("data", "repo-data", ".git", ".venv")              # a release never carries these


# --------------------------------------------------------------------------------- small helpers
def log(msg: str) -> None:
    print(f"  {msg}", flush=True)


def step(n: int, what: str) -> None:
    print(f"\n[{n}/5] {what}", flush=True)


def ver_key(tag: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", tag or "")) or (0,)


def read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


class Paths:
    """Everything this device keeps, under one roof, with the data outside the code."""

    def __init__(self, home: str):
        self.home = os.path.abspath(os.path.expanduser(home))
        os.makedirs(self.home, exist_ok=True)

    @property
    def data(self) -> str:
        return os.path.join(self.home, "data")

    @property
    def versions(self) -> str:
        return os.path.join(self.home, "versions")

    @property
    def conf(self) -> str:
        return os.path.join(self.home, "launcher.json")

    @property
    def token_file(self) -> str:
        return os.path.join(self.home, "token.txt")

    @property
    def installed_file(self) -> str:
        return os.path.join(self.home, "installed.json")

    def installed(self) -> dict:
        return read_json(self.installed_file, {})

    def code(self, tag: str = "") -> str:
        tag = tag or self.installed().get("tag") or ""
        return os.path.join(self.versions, tag) if tag else ""


def default_home() -> str:
    env = os.environ.get(f"{APP_SLUG.upper()}_HOME")
    if env:
        return env
    if IOS:                                                    # Pythonista: beside the script, in Documents
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), APP_NAME)
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP_NAME)


def ask(prompt: str, secret: bool = False) -> str:
    """A dialog where there is one, the console where there is not."""
    try:
        import tkinter
        from tkinter import simpledialog
        root = tkinter.Tk()
        root.withdraw()
        value = simpledialog.askstring(APP_NAME, prompt, show="*" if secret else None, parent=root)
        root.destroy()
        if value is not None:
            return value.strip()
    except Exception:                                          # noqa: BLE001  (no display, no tkinter, iOS)
        pass
    try:
        return input(f"{prompt}: ").strip()
    except EOFError:
        return ""


# --------------------------------------------------------------------------------- 1. finding the newest code
class Source:
    """Where a version is coming from: a GitHub release, a zip on disk, or an unpacked folder."""

    def __init__(self, kind: str, tag: str, ref):
        self.kind, self.tag, self.ref = kind, tag, ref

    def __str__(self) -> str:
        where = {"github": "GitHub", "zip": os.path.basename(str(self.ref)), "folder": "this folder"}
        return f"{self.tag} from {where[self.kind]}"


class DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """GitHub answers an asset download with a redirect to a storage host carrying its own signature. urllib
    forwards the Authorization header across that redirect, which storage rejects — and which would hand this
    device's token to a third party. Neither is wanted: drop it when the host changes."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            new.headers = {k: v for k, v in new.headers.items() if k.lower() != "authorization"}
            new.unredirected_hdrs = {k: v for k, v in getattr(new, "unredirected_hdrs", {}).items()
                                     if k.lower() != "authorization"}
        return new


OPENER = urllib.request.build_opener(DropAuthOnRedirect)


def gh(url: str, token: str, accept: str = "application/vnd.github+json"):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": f"{APP_SLUG}/{LAUNCHER_VERSION}",
                                               **({"Authorization": f"Bearer {token}"} if token else {})})
    return OPENER.open(req, timeout=HTTP_TIMEOUT)


def github_source(repo: str, token: str) -> Source | None:
    if not repo:
        return None
    try:
        with gh(f"{API}/repos/{repo}/releases/latest", token) as r:
            rel = json.load(r)
    except Exception as e:                                     # noqa: BLE001
        log(f"GitHub not reachable ({e})")
        return None
    return Source("github", rel.get("tag_name", ""), rel) if rel.get("tag_name") else None


def zip_version(path: str) -> str:
    """The tag a zip really carries. The filename is a hint; what is inside it is the truth: a VERSION, an
    appinfo.json claiming this app, and the entry file that appinfo names."""
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if not {"VERSION", "appinfo.json"} <= names:
                return ""
            app = json.loads(z.read("appinfo.json").decode("utf-8"))
            if app.get("slug") != APP_SLUG or (app.get("entry") or ENTRY) not in names:
                return ""
            return z.read("VERSION").decode("utf-8").strip()
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        return ""


def zip_source(folders: list) -> Source | None:
    """The newest <slug>-vN.zip lying in Downloads (or wherever else was named)."""
    pattern = re.compile(rf"^{re.escape(APP_SLUG)}[-_]v?[\d.]+.*\.zip$", re.I)
    best = None
    for folder in folders:
        if not folder or not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            if not pattern.match(name):
                continue
            path = os.path.join(folder, name)
            tag = zip_version(path)
            if not tag:
                log(f"ignoring {name}: not a {APP_NAME} release zip")
                continue
            if best is None or ver_key(tag) > ver_key(best.tag):
                best = Source("zip", tag, path)
    return best


def folder_source(folder: str) -> Source | None:
    """The launcher was unpacked from a zip and run where it landed: install from here."""
    version_file, entry_file = os.path.join(folder, "VERSION"), os.path.join(folder, ENTRY)
    if not (os.path.isfile(version_file) and os.path.isfile(entry_file)):
        return None
    with open(version_file, encoding="utf-8") as f:
        return Source("folder", f.read().strip(), folder)


def find_source(kind: str, paths: Paths, repo: str, token: str, downloads: list) -> Source | None:
    here = os.path.dirname(os.path.abspath(__file__))
    if kind == "github":
        return github_source(repo, token)
    if kind == "zip":
        return zip_source(downloads) or folder_source(here)
    if kind == "folder":
        return folder_source(here)
    return github_source(repo, token) or zip_source(downloads) or folder_source(here)


# --------------------------------------------------------------------------------- 2. installing it
def safe_member(name: str) -> bool:
    p = os.path.normpath(name).replace("\\", "/")
    return bool(p) and not p.startswith(("/", "..")) and p.split("/")[0] not in KEEP_OUT and ".." not in p.split("/")


def unpack(blob: bytes, target: str) -> None:
    """Unpack into `target`, dropping GitHub's wrapper folder if there is one.

    Two layouts arrive here. A release zip has the app's files at the top, the way make_zip builds it. A
    repository archive — what GitHub serves when a release has no zip attached to it — has them under `app/`.
    Both have to end up looking the same, because that is what the launcher runs.
    """
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        if not names:
            raise ValueError("empty zip")
        first = names[0].split("/")[0] + "/" if "/" in names[0] else ""
        strip = first if first and all(n.startswith(first) for n in names) else ""
        wanted = [(n, n[len(strip):]) for n in names]
        inside = {rel for _n, rel in wanted}
        if "appinfo.json" not in inside and "app/appinfo.json" in inside:
            wanted = [(n, rel[4:] if rel.startswith("app/") else rel) for n, rel in wanted]
        refused = [rel for _n, rel in wanted if not safe_member(rel)]
        os.makedirs(target, exist_ok=True)
        for name, rel in wanted:
            if not safe_member(rel):
                continue                                       # never escapes the folder, never carries data
            dest = os.path.join(target, rel)
            os.makedirs(os.path.dirname(dest) or target, exist_ok=True)
            with open(dest, "wb") as f:
                f.write(z.read(name))
        if refused:
            log(f"{len(refused)} file(s) in the zip were refused: {', '.join(refused[:3])}")


def fetch(src: Source, token: str) -> bytes:
    if src.kind == "github":
        asset = next((a for a in src.ref.get("assets", []) if a["name"].endswith(".zip")), None)
        url = asset["url"] if asset else src.ref["zipball_url"]
        with gh(url, token, "application/octet-stream" if asset else "application/vnd.github+json") as r:
            return r.read()
    with open(src.ref, "rb") as f:
        return f.read()


def install(src: Source, paths: Paths, token: str, force: bool = False) -> str:
    target = os.path.join(paths.versions, src.tag)
    if src.kind == "folder":
        if os.path.abspath(src.ref) == os.path.abspath(target):
            return target
        if force or not os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(src.ref, target, ignore=shutil.ignore_patterns(*KEEP_OUT, "__pycache__"))
            log(f"installed {src.tag} from the folder it was unpacked into")
    elif force or not os.path.isdir(target):
        log(f"downloading {src}" if src.kind == "github" else f"unpacking {src}")
        shutil.rmtree(target, ignore_errors=True)
        unpack(fetch(src, token), target)
    state = paths.installed()
    if state.get("tag") and state["tag"] != src.tag:
        state["previous"] = state["tag"]
    state.update(tag=src.tag, source=src.kind, at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    write_json(paths.installed_file, state)
    prune(paths, {src.tag, state.get("previous") or "", state.get("pin") or ""})
    return target


def prune(paths: Paths, keep: set) -> None:
    """Two versions and whatever is pinned; the rest goes."""
    if not os.path.isdir(paths.versions):
        return
    for name in os.listdir(paths.versions):
        if name not in keep:
            shutil.rmtree(os.path.join(paths.versions, name), ignore_errors=True)


# --------------------------------------------------------------------------------- 3. the launcher itself
IDENTITY = re.compile(r"# --- identity.*?# --- end identity[^\n]*\n", re.S)


def update_self(code_dir: str) -> bool:
    """The code carries launcher.py. If its copy is newer, write it here — keeping this device's identity,
    which is what pins the data folder to the same place across a rename. The caller then starts again."""
    src = os.path.join(code_dir, "launcher.py")
    me = os.path.abspath(__file__)
    if not os.path.isfile(src) or os.path.abspath(src) == me:
        return False
    try:
        with open(src, encoding="utf-8") as f:
            text = f.read()
        found = re.search(r'LAUNCHER_VERSION\s*=\s*"([^"]+)"', text)
        if not found or ver_key(found.group(1)) <= ver_key(LAUNCHER_VERSION):
            return False
        compile(text, src, "exec")                             # an unparsable launcher never replaces this one
        with open(me, encoding="utf-8") as f:
            mine = f.read()
        block = IDENTITY.search(mine)
        if block and IDENTITY.search(text):
            text = IDENTITY.sub(lambda _m: block.group(0), text, count=1)
        backup = f"{me}.{LAUNCHER_VERSION}.bak"
        shutil.copy2(me, backup)
        tmp = me + ".new"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, me)
        log(f"launcher {LAUNCHER_VERSION} -> {found.group(1)} (previous copy: {os.path.basename(backup)})")
        return True
    except Exception as e:                                     # noqa: BLE001
        log(f"launcher self-update skipped: {e!r}")
        return False


def refresh_icons(code_dir: str, beside: str) -> None:
    """Keep the double-click files next to the launcher, and current. A device that arrived by one route ends
    up holding both, so the online laptop can fall back to a zip and the offline one is ready if it ever gets
    a token."""
    for name in ICONS:
        src, dest = os.path.join(code_dir, name), os.path.join(beside, name)
        if not os.path.isfile(src):
            continue
        try:
            if os.path.isfile(dest):
                with open(src, "rb") as a, open(dest, "rb") as b:
                    if a.read() == b.read():
                        continue
            shutil.copy2(src, dest)
            if name.endswith(".command"):
                os.chmod(dest, 0o755)
        except OSError:
            continue


def ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def desktop_shortcut(paths: Paths, code_dir: str, offline: bool) -> None:
    """After the first run that worked, put an icon on the desktop. Every start after that is one click:
    the shortcut runs the same wrapper, which runs the launcher, which updates itself, the app and the data.

    Made once. If it gets deleted that was a decision, so it is not put back."""
    here = os.path.dirname(os.path.abspath(__file__))
    name = f"{APP_NAME} (offline)" if offline else APP_NAME
    made = os.path.join(paths.home, ".shortcut-offline" if offline else ".shortcut")
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    if os.path.exists(made) or not os.path.isdir(desktop):
        return
    for kind in ("ico", "png"):                                # a stable path, so an update never breaks it
        art = os.path.join(code_dir, "static", f"icon.{kind}")
        if os.path.isfile(art):
            shutil.copy2(art, os.path.join(paths.home, f"icon.{kind}"))
    try:
        if os.name == "nt":
            target = os.path.join(here, "Start-Offline.bat" if offline else "Start.bat")
            icon = os.path.join(paths.home, "icon.ico")
            script = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut({ps_quote(os.path.join(desktop, name + '.lnk'))});"
                      f"$s.TargetPath={ps_quote(target)};$s.WorkingDirectory={ps_quote(here)};"
                      f"$s.Description={ps_quote(name)};"
                      + (f"$s.IconLocation={ps_quote(icon)};" if os.path.isfile(icon) else "")
                      + "$s.Save()")
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, timeout=60, check=True)
        elif sys.platform == "darwin":
            link = os.path.join(desktop, name)
            if not os.path.lexists(link):
                os.symlink(os.path.join(here, "Start-Offline.command" if offline else "Start.command"), link)
        else:
            entry = os.path.join(desktop, f"{APP_SLUG}{'-offline' if offline else ''}.desktop")
            with open(entry, "w", encoding="utf-8", newline="\n") as f:
                f.write("[Desktop Entry]\nType=Application\nVersion=1.0\n"
                        f"Name={name}\nComment={name}\n"
                        f"Exec=\"{os.path.join(here, 'Start-Offline.command' if offline else 'Start.command')}\"\n"
                        f"Icon={os.path.join(paths.home, 'icon.png')}\n"
                        f"Path={here}\nTerminal=true\n")
            os.chmod(entry, 0o755)
        with open(made, "w", encoding="utf-8") as f:
            f.write(name + "\n")
        log(f"put {name} on the desktop — one click from now on")
    except Exception as e:                                     # noqa: BLE001  never worth failing a run over
        log(f"could not make the desktop shortcut: {e}")


def restart(argv: list) -> None:
    argv = [a for a in argv if a != "--restarted"] + ["--restarted"]
    me = os.path.abspath(__file__)
    if IOS:                                                    # Pythonista cannot exec; re-enter in-process
        sys.argv = [me] + argv
        with open(me, encoding="utf-8") as f:
            code = compile(f.read(), me, "exec")
        raise SystemExit(exec(code, {"__name__": "__main__", "__file__": me}))  # noqa: S102
    os.execv(sys.executable, [sys.executable, me] + argv)


# --------------------------------------------------------------------------------- 4. running the app
def app_info(code_dir: str) -> dict:
    d = read_json(os.path.join(code_dir, "appinfo.json"), {})
    return {"name": d.get("name") or APP_NAME, "slug": d.get("slug") or APP_SLUG,
            "port": int(d.get("port") or DEFAULT_PORT), "entry": d.get("entry") or ENTRY,
            "repo": d.get("repo") or DEFAULT_REPO}


def python_for(code_dir: str, paths: Paths) -> str:
    """This interpreter, unless the app declares dependencies — then a virtual environment, made once."""
    req = os.path.join(code_dir, "requirements.txt")
    needs = os.path.isfile(req) and bool(open(req, encoding="utf-8").read().strip())
    if not needs or IOS:
        return sys.executable
    venv = os.path.join(paths.home, ".venv")
    py = os.path.join(venv, "Scripts", "python.exe") if os.name == "nt" else os.path.join(venv, "bin", "python")
    if not os.path.isfile(py):
        log("making the environment (once)")
        subprocess.run([sys.executable, "-m", "venv", venv], check=True)
    subprocess.run([py, "-m", "pip", "install", "-q", "-r", req], check=False)
    return py


def serving(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
            return r.status < 500
    except Exception:                                          # noqa: BLE001
        return False


def env_for(paths: Paths, info: dict, token: str, repo: str, transport: str) -> dict:
    return {**os.environ, "APP_DATA_DIR": paths.data, "APP_PORT": str(info["port"]),
            "APP_TRANSPORT": transport, "GH_TOKEN": token, "GH_REPO": repo,
            f"{info['slug'].upper()}_HOME": paths.home}


def wait_until_serving(port: int, alive=lambda: True, seconds: int = 60) -> bool:
    for _ in range(seconds * 4):
        if serving(port):
            return True
        if not alive():
            return False
        time.sleep(0.25)
    return False


def run_desktop(py: str, code_dir: str, info: dict, env: dict, browser: bool, smoke: bool) -> int:
    proc = subprocess.Popen([py, info["entry"]], cwd=code_dir, env=env)
    ok = wait_until_serving(info["port"], alive=lambda: proc.poll() is None)
    url = f"http://127.0.0.1:{info['port']}/"
    if not ok:
        log("the app did not answer")
        proc.terminate()
        return 1
    log(f"{info['name']} is at {url}")
    if smoke:
        proc.terminate()
        return 0
    if browser:
        try:
            webbrowser.open(url)
        except Exception:                                      # noqa: BLE001
            pass
    log("close the window (or press Stop on the page) and the data goes back")
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
    return 0


def run_ios(code_dir: str, info: dict, env: dict, browser: bool, smoke: bool) -> int:
    """Pythonista has no subprocesses: the app runs in this process and the web view is the window."""
    os.environ.update(env)
    os.chdir(code_dir)
    sys.path.insert(0, code_dir)
    for name in ("app", "frame", "datasync", os.path.splitext(info["entry"])[0]):
        sys.modules.pop(name, None)
    module = __import__(os.path.splitext(info["entry"])[0])
    server = module.make_server(info["port"])
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{info['port']}/"
    if not wait_until_serving(info["port"]):
        log("the app did not answer")
        server.shutdown()
        return 1
    log(f"{info['name']} is at {url}")
    if smoke:
        server.shutdown()
        return 0
    try:
        import ui                                              # noqa: F401  (Pythonista only)
        view = ui.WebView(name=info["name"])
        view.load_url(url)
        view.present("fullscreen")
        view.wait_modal()                                      # closing the view is closing the window
    except ImportError:
        if browser:
            webbrowser.open(url)
        log("close the window (or press Stop on the page) and the data goes back")
        while serving(info["port"]):
            time.sleep(1)
    server.shutdown()
    return 0


def fall_back(paths: Paths, token: str, repo: str, browser: bool, smoke: bool) -> tuple:
    """A version that will not start must not leave a device with a dead icon and no way back. Mark it,
    return to the one before it, and remember not to install it again unless asked with --force."""
    state = paths.installed()
    broken, previous = state.get("tag") or "", state.get("previous") or ""
    if not previous or not os.path.isdir(paths.code(previous)):
        return 1, ""
    log(f"{broken} did not start; falling back to {previous}")
    state["bad"] = sorted(set(state.get("bad", []) + [broken]))
    state["tag"], state["previous"] = previous, broken
    write_json(paths.installed_file, state)
    code_dir = paths.code(previous)
    info = app_info(code_dir)
    env = env_for(paths, info, token, repo, state.get("source") or "github")
    return run_desktop(python_for(code_dir, paths), code_dir, info, env, browser, smoke), code_dir


# --------------------------------------------------------------------------------- 3 & 5. the data
def sync(py: str, code_dir: str, direction: str, env: dict) -> None:
    if IOS:
        sys.path.insert(0, code_dir)
        sys.modules.pop("datasync", None)
        os.environ.update(env)
        import datasync
        result = datasync.pull() if direction == "pull" else datasync.push()
        log(f"data {direction}: {json.dumps(result)[:160]}")
        return
    run = subprocess.run([py, "datasync.py", direction], cwd=code_dir, text=True, capture_output=True,
                         env=env, timeout=600)
    for line in (run.stdout or "").strip().splitlines():
        log(line)
    if run.returncode and (run.stderr or "").strip():
        log((run.stderr or "").strip().splitlines()[-1][:200])


# --------------------------------------------------------------------------------- first run
def check_token(repo: str, token: str) -> str:
    """Empty if the token can see the repository, otherwise why not. Asked once, on the run that sets it up,
    so a bad paste says so immediately instead of surfacing later as 'GitHub not reachable'."""
    try:
        with gh(f"{API}/repos/{repo}", token) as r:
            json.load(r)
        return ""
    except urllib.error.HTTPError as e:
        return {401: "the token was refused", 403: "the token may not read this repository",
                404: f"no repository {repo}, or the token cannot see it"}.get(e.code, f"GitHub said {e.code}")
    except Exception as e:                                     # noqa: BLE001
        return str(e)


def configure(args, paths: Paths) -> tuple:
    conf = read_json(paths.conf, {})
    if args.reset:
        conf = {}
        for p in (paths.conf, paths.token_file):
            if os.path.exists(p):
                os.remove(p)
    repo = conf.get("repo") or DEFAULT_REPO
    token = os.environ.get("GH_TOKEN", "")
    if os.path.isfile(paths.token_file):
        with open(paths.token_file, encoding="utf-8") as f:
            token = f.read().strip() or token
    wants_github = args.source in ("auto", "github") and not args.offline
    if wants_github and not repo:
        repo = ask("GitHub repository (owner/name)")
    if wants_github and repo and not token:
        print(f"\n{APP_NAME} lives in the private repository {repo}, and needs your GitHub token to download\n"
              f"it and to sync its data. Fine-grained token, Contents: read and write.\n"
              f"(Leave it empty to run without GitHub, from whatever is already on this device.)", flush=True)
        for attempt in (1, 2):
            token = ask(f"GitHub token for {repo}")
            if not token:
                log("no token: carrying on without GitHub")
                break
            why = check_token(repo, token)
            if not why:
                with open(paths.token_file, "w", encoding="utf-8", newline="\n") as f:
                    f.write(token + "\n")
                log("token accepted, and kept in token.txt — it will not ask again")
                break
            log(f"that token did not work: {why}")
            token = "" if attempt == 2 else token
    write_json(paths.conf, {"repo": repo, "launcher": LAUNCHER_VERSION})
    return repo, token


# --------------------------------------------------------------------------------- main
def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    ap = argparse.ArgumentParser(description=f"{APP_NAME} launcher {LAUNCHER_VERSION}", add_help=True)
    ap.add_argument("--source", choices=("auto", "github", "zip", "folder"), default="auto")
    for flag in ("--offline", "--no-push", "--check", "--rollback", "--reset", "--force", "--smoke",
                 "--restarted", "--no-browser"):
        ap.add_argument(flag, action="store_true")
    ap.add_argument("--pin", default="")
    ap.add_argument("--home", default="")
    ap.add_argument("--downloads", default="")
    a = ap.parse_args(argv)
    if IOS and a.source == "auto":
        a.source = "github"

    paths = Paths(a.home or default_home())
    os.makedirs(paths.data, exist_ok=True)
    print(f"{APP_NAME} launcher {LAUNCHER_VERSION} · {paths.home}", flush=True)
    repo, token = configure(a, paths)
    state = paths.installed()

    if a.pin:
        state["pin"] = "" if a.pin == "off" else a.pin
        write_json(paths.installed_file, state)
        log("pin cleared" if not state["pin"] else f"pinned to {state['pin']}")
    if a.rollback and state.get("previous"):
        state["tag"], state["previous"] = state["previous"], state.get("tag")
        write_json(paths.installed_file, state)
        log(f"rolled back to {state['tag']}")

    downloads = [a.downloads] if a.downloads else [os.path.join(os.path.expanduser("~"), "Downloads"),
                                                   os.path.join(os.path.expanduser("~"), "Desktop"),
                                                   os.path.dirname(os.path.abspath(__file__))]

    step(1, "the newest code")
    src = None if a.offline and a.source in ("auto", "github") else find_source(a.source, paths, repo, token, downloads)
    if a.check:
        print(f"installed {state.get('tag') or 'nothing'} · available {src or 'nothing'} · "
              f"launcher {LAUNCHER_VERSION} · home {paths.home}")
        return 0
    if src:
        log(f"found {src}")
    else:
        log("nothing new available; using what is installed")

    step(2, "the code, and the launcher itself")
    if src and src.tag in state.get("bad", []) and not a.force:
        log(f"{src.tag} did not start last time; staying on {state.get('tag')} (--force to try it again)")
        src = None
    if src and (a.force or ver_key(src.tag) >= ver_key(state.get("tag") or "")):
        code_now = install(src, paths, token, force=a.force)
        refresh_icons(code_now, os.path.dirname(os.path.abspath(__file__)))
        if not a.restarted and update_self(code_now):
            restart(argv)
    else:
        log(f"launcher {LAUNCHER_VERSION}, unchanged")

    pin = state.get("pin") or ""
    code_dir = paths.code(pin) if pin else paths.code()
    if pin and not os.path.isdir(code_dir):
        log(f"pinned to {pin}, which is not installed; using the newest")
        code_dir = paths.code()
    if not code_dir or not os.path.isdir(code_dir):
        log(f"nothing installed and no source to install from — put a {APP_SLUG}-vN.zip in Downloads, "
            f"or give the launcher a repository")
        return 1
    info = app_info(code_dir)
    repo = repo or info["repo"]
    transport = "ios" if IOS else (state.get("source") or (src.kind if src else "folder"))
    log(f"running {os.path.basename(code_dir)}{' (pinned)' if pin else ''}")
    if not IOS:
        desktop_shortcut(paths, code_dir, offline=transport == "zip")
    py = python_for(code_dir, paths)
    env = env_for(paths, info, token, repo, transport)

    step(3, "the data")
    linked = bool(token and repo) and not a.offline
    if linked:
        sync(py, code_dir, "pull", env)
    else:
        log("no repository or token on this device: the local data folder as it is")

    step(4, info["name"])
    browser = not a.no_browser and not a.smoke
    if IOS:
        rc = run_ios(code_dir, info, env, browser, a.smoke)
    else:
        rc = run_desktop(py, code_dir, info, env, browser, a.smoke)
        if rc and not pin:
            rc, recovered = fall_back(paths, token, repo, browser, a.smoke)
            code_dir = recovered or code_dir

    step(5, "the data, on its way back")
    if a.no_push or not linked:
        log("not pushing")
    else:
        sync(py, code_dir, "push", env)
    if a.smoke:
        print(f"smoke: {'serving' if rc == 0 else 'did not answer'}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
