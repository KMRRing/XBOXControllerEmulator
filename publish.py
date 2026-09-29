"""publish — cut a version. A window with two buttons, or two arguments if you prefer.

    double-click Publish.bat / Publish.command      a window: version, notes, Publish or Save zip
    python publish.py v0.2 "what changed"           the same thing without the window
    python publish.py v0.2 "what changed" --zip     only the zip, into Downloads (no git, no GitHub)

Publishing does five things: writes `app/VERSION`, copies the app's identity into `launcher.py` so a fresh
device is stamped with it, commits and tags, pushes, and uploads the zip to the release. That zip is what
every device reads — the GitHub launchers download it, and the offline laptop gets the same file by hand.

The token needs Contents: read and write, plus permission to create releases. It is remembered in
`<home>/.<slug>-publish-token` so it is asked for once.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request

import make_zip

ROOT = os.path.dirname(os.path.abspath(__file__))
API = os.environ.get("GH_API") or "https://api.github.com"
IDENTITY = re.compile(r"# --- identity.*?# --- end identity[^\n]*\n", re.S)


def info() -> dict:
    return make_zip.info()


def token_path() -> str:
    return os.path.join(os.path.expanduser("~"), f".{info()['slug']}-publish-token")


def token(given: str = "") -> str:
    if given:
        with open(token_path(), "w", encoding="utf-8", newline="\n") as f:
            f.write(given.strip() + "\n")
        return given.strip()
    if os.environ.get("GH_TOKEN"):
        return os.environ["GH_TOKEN"]
    try:
        with open(token_path(), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def next_version() -> str:
    parts = re.findall(r"\d+", make_zip.version())
    if not parts:
        return "v0.1"
    parts[-1] = str(int(parts[-1]) + 1)
    return "v" + ".".join(parts)


def stamp_launcher() -> None:
    """One source of truth: app/appinfo.json writes the identity into the launcher and into the two online
    wrappers, which need the repository name before there is any code on the machine to read it from."""
    app = info()
    path = os.path.join(ROOT, "launcher.py")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    block = ('# --- identity: publish.py writes this block from app/appinfo.json '
             + '-' * 26 + "\n"
             f'APP_NAME = "{app["name"]}"\n'
             f'APP_SLUG = "{app["slug"]}"\n'
             f'DEFAULT_REPO = "{app["repo"]}"\n'
             f'DEFAULT_PORT = {int(app["port"])}\n'
             f'ENTRY = "{app.get("entry", "app.py")}"\n'
             '# --- end identity ' + '-' * 90 + "\n")
    new = IDENTITY.sub(lambda _m: block, text, count=1)
    if new != text:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(new)
    stamp_wrappers(app)


WRAPPER_IDENTITY = re.compile(r"((?:rem|#) --- identity[^\n]*\n).*?((?:rem|#) --- end identity[^\n]*\n)", re.S)


def stamp_wrappers(app: dict) -> None:
    for name, quote in (("Start.bat", 'set "{}={}"'), ("Start.command", '{}="{}"')):
        path = os.path.join(ROOT, name)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            text = f.read()

        def fill(m, quote=quote, app=app):
            body = quote.format("APP_NAME", app["name"]) + "\n" + quote.format("APP_REPO", app["repo"]) + "\n"
            return m.group(1) + body + m.group(2)

        new = WRAPPER_IDENTITY.sub(fill, text, count=1)
        if new != text:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(new)


def set_version(tag: str) -> None:
    with open(os.path.join(ROOT, "app", "VERSION"), "w", encoding="utf-8", newline="\n") as f:
        f.write(tag + "\n")


def downloads() -> str:
    d = os.path.join(os.path.expanduser("~"), "Downloads")
    return d if os.path.isdir(d) else os.path.expanduser("~")


def git(*args: str) -> str:
    done = subprocess.run(("git",) + args, cwd=ROOT, text=True, capture_output=True)
    if done.returncode:
        raise RuntimeError((done.stderr or done.stdout).strip().splitlines()[-1] if (done.stderr or done.stdout)
                           else f"git {' '.join(args)} failed")
    return done.stdout.strip()


def signature() -> tuple:
    """A machine that has never used git has no name or address, and refuses to commit or tag. That is not a
    reason to stop a release, so supply one for this commit only."""
    probe = subprocess.run(("git", "config", "user.email"), cwd=ROOT, text=True, capture_output=True)
    if probe.returncode == 0 and probe.stdout.strip():
        return ()
    app = info()
    return ("-c", f"user.email={app['slug']}@localhost", "-c", f"user.name={app['name']} publish")


def gh(method: str, url: str, tok: str, body=None, ctype: str = "application/json"):
    data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "publish",
                                          "Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r) if r.headers.get_content_type() == "application/json" else r.read()


# --------------------------------------------------------------------------------- the two things it can do
def save_zip(tag: str, out_dir: str = "") -> str:
    app = info()
    set_version(tag)
    stamp_launcher()
    out = os.path.join(out_dir or downloads(), f"{app['slug']}-{tag}.zip")
    make_zip.build(out)
    return out


def publish(tag: str, notes: str, tok: str) -> str:
    app = info()
    if not tok:
        raise RuntimeError("a token is needed (Contents: read and write)")
    set_version(tag)
    stamp_launcher()
    git("add", "app/VERSION", "launcher.py", "Start.bat", "Start.command")
    if git("status", "--porcelain", "app/VERSION", "launcher.py", "Start.bat", "Start.command"):
        git(*signature(), "commit", "-q", "-m", f"release {tag}")
    remote = f"https://x-access-token:{tok}@github.com/{app['repo']}.git"
    branch = git("rev-parse", "--abbrev-ref", "HEAD") or "main"
    # Every device that closes the app commits its data (repo-data/) to this branch, so GitHub is usually ahead
    # by a few data commits. They touch nothing of the code: take them, and put the release on top.
    try:
        git("fetch", "-q", remote, branch)
    except RuntimeError:
        pass                                                   # a first push: nothing there yet
    else:
        git(*signature(), "rebase", "-q", "FETCH_HEAD")
    git(*signature(), "tag", "-a", tag, "-m", f"{app['name']} {tag}")
    git("push", "-q", remote, f"HEAD:{branch}")
    git("push", "-q", remote, tag)
    release = gh("POST", f"{API}/repos/{app['repo']}/releases", tok,
                 {"tag_name": tag, "name": f"{app['name']} {tag}", "body": notes})
    zip_path = os.path.join(ROOT, f"{app['slug']}-{tag}.zip")
    kept = os.path.join(downloads(), f"{app['slug']}-{tag}.zip")
    note = ""
    try:
        make_zip.build(zip_path)
        shutil.copy2(zip_path, kept)                           # the offline laptop's copy, whatever follows
        with open(zip_path, "rb") as f:
            blob = f.read()
        upload = release["upload_url"].split("{")[0] + f"?name={app['slug']}-{tag}.zip"
        try:
            gh("POST", upload, tok, blob, ctype="application/zip")
        except Exception as e:                                 # noqa: BLE001
            # uploads.github.com is a different host from the API, and is not always reachable — from a
            # sandbox, or behind a proxy. The release is already made, and a device without an attached zip
            # falls back to the source archive, so this is a note rather than a failure.
            note = (f"\nThe zip was not attached ({e}). The release works: devices fall back to the source"
                    f" archive. The zip for the offline laptop is at {kept}.")
    finally:
        if os.path.exists(zip_path):
            os.remove(zip_path)
    return release["html_url"] + note


# --------------------------------------------------------------------------------- the window
def window() -> int:
    import tkinter as tk
    from tkinter import messagebox

    app = info()
    root = tk.Tk()
    root.title(f"Publish {app['name']}")
    root.resizable(False, False)
    pad = {"padx": 10, "pady": 4}
    fields = {}
    for row, (label, value, hide) in enumerate((("Version", next_version(), False),
                                                ("What changed", "", False),
                                                ("Token", token(), True))):
        tk.Label(root, text=label, anchor="w", width=12).grid(row=row, column=0, sticky="w", **pad)
        entry = tk.Entry(root, width=46, show="*" if hide and token() else "")
        entry.insert(0, value)
        entry.grid(row=row, column=1, columnspan=2, **pad)
        fields[label] = entry
    status = tk.Label(root, text=f"Now: {make_zip.version()} · {app['repo']}", anchor="w", fg="#555")
    status.grid(row=3, column=0, columnspan=3, sticky="w", **pad)

    def guard(fn):
        def run():
            tag = fields["Version"].get().strip()
            if not re.match(r"^v[\d.]+$", tag):
                return messagebox.showerror(app["name"], "The version looks like v0.2")
            status.config(text="Working…")
            root.update()
            try:
                message = fn(tag)
            except Exception as e:                             # noqa: BLE001
                status.config(text="Stopped.")
                return messagebox.showerror(app["name"], str(e))
            status.config(text=message)
            messagebox.showinfo(app["name"], message)
            return None
        return run

    tk.Button(root, text="Publish to GitHub", width=18,
              command=guard(lambda tag: f"Published {tag}\n"
                                        + publish(tag, fields["What changed"].get().strip(),
                                                  token(fields["Token"].get().strip())))
              ).grid(row=4, column=1, sticky="e", **pad)
    tk.Button(root, text="Save zip only", width=14,
              command=guard(lambda tag: f"Saved {save_zip(tag)}")).grid(row=4, column=2, sticky="e", **pad)
    root.mainloop()
    return 0


def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv:
        return window()
    tag = argv[0]
    if not re.match(r"^v[\d.]+$", tag):
        sys.exit(__doc__)
    notes = next((a for a in argv[1:] if not a.startswith("--")), "")
    if "--zip" in argv:
        print(save_zip(tag))
        return 0
    print(publish(tag, notes, token()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
