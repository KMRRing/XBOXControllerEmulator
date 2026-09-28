"""The system's own tests: the three ways in, run for real.

A stub GitHub stands in for the real one — releases and the contents API, in memory — so the whole cycle can
be exercised end to end: the launcher finds a version, replaces itself, installs the code, pulls the data,
starts the app, and pushes what changed when the app stops.

    python -m pytest test_system.py -q
"""
from __future__ import annotations

import base64
import http.server
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
INFO = json.loads(open(os.path.join(HERE, "app", "appinfo.json"), encoding="utf-8").read())
SLUG, SIGN = INFO["slug"], INFO["name"]          # the tests follow a rename instead of breaking on it


# --------------------------------------------------------------------------------- a GitHub in a dict
class Hub:
    """Releases and a contents API. Enough for the launcher and for datasync."""

    def __init__(self):
        self.releases, self.contents, self.port, self._srv = [], {}, 0, None
        self.storage_port, self._storage = 0, None
        self.token = "t0k3n"                   # a private repository: the wrong token sees nothing
        self.leaked_token = False              # set if a request to storage carried an Authorization header

    def add_release(self, tag: str, blob: bytes, source: bytes = b"", asset: bool = True):
        self.releases.append({"tag_name": tag, "zip": blob, "source": source or blob, "asset": asset})

    @property
    def api(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        hub = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):                                  # noqa: N802
                if self.path.endswith("/releases/latest"):
                    if not hub.releases:
                        return self._send(404, b'{"message":"none"}')
                    rel = hub.releases[-1]
                    return self._send(200, json.dumps({
                        "tag_name": rel["tag_name"],
                        "zipball_url": f"{hub.api}/zip/{rel['tag_name']}",
                        "assets": ([{"name": f"{SLUG}-{rel['tag_name']}.zip",
                                     "url": f"{hub.api}/asset/{rel['tag_name']}"}] if rel["asset"] else [])
                        }).encode())
                if self.path.startswith("/asset/"):
                    # what GitHub really does: a redirect to a storage host that signs its own URLs
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{hub.storage_port}{self.path}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                m = re.match(r"^/(zip|asset)/(.+)$", self.path)
                if m:
                    rel = next(r for r in hub.releases if r["tag_name"] == m.group(2))
                    body = rel["source"] if m.group(1) == "zip" else rel["zip"]
                    return self._send(200, body, "application/octet-stream")
                m = re.match(r"^/repos/[^/]+/[^/]+/contents/(.+)$", self.path)
                if m:
                    if m.group(1) not in hub.contents:
                        return self._send(404, b'{"message":"Not Found"}')
                    body = hub.contents[m.group(1)]
                    return self._send(200, json.dumps({"content": base64.b64encode(body).decode(),
                                                       "sha": f"sha-{len(body)}"}).encode())
                if re.match(r"^/repos/[^/]+/[^/]+$", self.path):
                    if self.headers.get("Authorization") != f"Bearer {hub.token}":
                        return self._send(404, b'{"message":"Not Found"}')
                    return self._send(200, b'{"full_name":"me/app"}')
                self._send(404, b'{"message":"?"}')

            def do_PUT(self):                                  # noqa: N802
                m = re.match(r"^/repos/[^/]+/[^/]+/contents/(.+)$", self.path)
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                hub.contents[m.group(1)] = base64.b64decode(body["content"])
                self._send(200, b'{"content":{}}')

        class Storage(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def do_GET(self):                                  # noqa: N802
                if self.headers.get("Authorization"):
                    hub.leaked_token = True                    # a signed URL rejects a second credential
                    self.send_response(400)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                rel = next(r for r in hub.releases if r["tag_name"] == self.path.rsplit("/", 1)[-1])
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(rel["zip"])))
                self.end_headers()
                self.wfile.write(rel["zip"])

        self._storage = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Storage)
        self.storage_port = self._storage.server_address[1]
        threading.Thread(target=self._storage.serve_forever, daemon=True).start()
        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self._srv.server_address[1]
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return self.api

    def stop(self):
        for server in (self._srv, self._storage):
            if server:
                server.shutdown()


# --------------------------------------------------------------------------------- fixtures and helpers
def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def repo(tmp_path):
    """A copy of this repository to cut releases from, on its own port."""
    dest = tmp_path / "repo"
    shutil.copytree(HERE, dest, ignore=shutil.ignore_patterns("__pycache__", ".git", "*.zip", ".pytest_cache"))
    info = json.loads((dest / "app" / "appinfo.json").read_text())
    info.update(repo="me/app", port=free_port())
    (dest / "app" / "appinfo.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    return dest


@pytest.fixture()
def hub():
    h = Hub()
    h.start()
    yield h
    h.stop()


def build(repo_dir, tag: str, launcher_version: str = "", app_name: str = "") -> bytes:
    """Cut a release exactly as publish.py would, optionally with a newer launcher inside it."""
    (repo_dir / "app" / "VERSION").write_text(tag + "\n", encoding="utf-8")
    if launcher_version or app_name:
        text = (repo_dir / "launcher.py").read_text(encoding="utf-8")
        if launcher_version:
            text = re.sub(r'^LAUNCHER_VERSION = "[^"]+"', f'LAUNCHER_VERSION = "{launcher_version}"',
                          text, count=1, flags=re.M)
        if app_name:
            text = re.sub(r'^APP_NAME = "[^"]+"', f'APP_NAME = "{app_name}"', text, count=1, flags=re.M)
        (repo_dir / "launcher.py").write_text(text, encoding="utf-8", newline="\n")
    out = repo_dir / f"{SLUG}-{tag}.zip"
    subprocess.run([PY, "make_zip.py", str(out)], cwd=repo_dir, check=True, text=True, capture_output=True)
    blob = out.read_bytes()
    out.unlink()
    return blob


def zipball(repo_dir, tag: str) -> bytes:
    """What GitHub serves for a release with no zip attached: the repository itself, in a wrapper folder,
    with the app still under app/."""
    (repo_dir / "app" / "VERSION").write_text(tag + "\n", encoding="utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for base, dirs, names in os.walk(repo_dir):
            dirs[:] = [d for d in dirs if d not in {"__pycache__", ".pytest_cache", ".git"}]
            for n in names:
                if n.endswith((".pyc", ".zip")):
                    continue
                full = os.path.join(base, n)
                z.write(full, f"me-app-{tag}abc/" + os.path.relpath(full, repo_dir).replace("\\", "/"))
    return buf.getvalue()


def device_with(repo_dir, folder):
    """What a device actually holds before anything has run: one file."""
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy2(repo_dir / "launcher.py", folder / "launcher.py")
    return folder / "launcher.py"


def launch(launcher, home, *args, hub=None, name="laptop", token="t0k3n", ios=False, timeout=180,
           typed=None):
    env = {**os.environ, "DEVICE_NAME": name, "HOME": str(home.parent)}
    os.makedirs(os.path.join(str(home.parent), "Desktop"), exist_ok=True)
    if hub:
        env["GH_API"] = hub.api
    if ios:
        env["LAUNCHER_FORCE_IOS"] = "1"
    if token:
        home.mkdir(parents=True, exist_ok=True)
        (home / "token.txt").write_text(token + "\n", encoding="utf-8")
    return subprocess.run([PY, str(launcher), "--home", str(home), "--no-browser", *args],
                          text=True, capture_output=True, timeout=timeout, env=env, input=typed)


def installed(home) -> dict:
    return json.loads((home / "installed.json").read_text())


# --------------------------------------------------------------------------------- 1. the app on its own
def test_the_app_keeps_its_data_outside_the_code(repo, tmp_path):
    port = json.loads((repo / "app" / "appinfo.json").read_text())["port"]
    data = tmp_path / "data"
    proc = subprocess.Popen([PY, "app.py"], cwd=repo / "app",
                            env={**os.environ, "APP_DATA_DIR": str(data), "DEVICE_NAME": "solo"})
    try:
        for _ in range(80):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=1) as r:
                    state = json.load(r)
                break
            except Exception:                                  # noqa: BLE001
                time.sleep(0.25)
        else:
            raise AssertionError("the app never answered")
        assert state["data"] == str(data) and "solo" in state["devices"]
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/api/quit", method="POST",
                                                      data=b"{}"), timeout=5)
    finally:
        proc.terminate()
    assert (data / "devices.json").exists(), "the only file it writes lands in the data folder"
    assert not (repo / "app" / "data").exists(), "and never beside the code"


def test_closing_the_page_stops_the_app(repo, tmp_path):
    """The heartbeat is what makes 'close the window' mean 'push the data'."""
    port = free_port()
    proc = subprocess.Popen([PY, "app.py"], cwd=repo / "app",
                            env={**os.environ, "APP_DATA_DIR": str(tmp_path / "d"), "APP_PORT": str(port),
                                 "APP_IDLE_SECONDS": "3"})
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except Exception:                                  # noqa: BLE001
                time.sleep(0.25)
        assert proc.poll() is None
        assert proc.wait(timeout=30) == 0, "it stops on its own once nothing is asking"
    finally:
        if proc.poll() is None:
            proc.terminate()


# --------------------------------------------------------------------------------- 2. GitHub, on a laptop
def test_github_transport_updates_the_launcher_the_code_and_the_data(repo, hub, tmp_path):
    hub.add_release("v0.1", build(repo, "v0.1"))
    launcher = device_with(repo, tmp_path / "laptop2")
    home = tmp_path / "home2"

    first = launch(launcher, home, "--smoke", hub=hub, name="laptop2")
    assert "smoke: serving" in first.stdout, first.stdout + first.stderr
    assert installed(home)["tag"] == "v0.1"
    assert (home / "versions" / "v0.1" / "app.py").exists()
    assert (home / "data" / "devices.json").exists(), "the app wrote into the launcher's data folder"
    assert "repo-data/devices.json" in hub.contents, "and step 5 put it on GitHub"
    assert b"laptop2" in hub.contents["repo-data/devices.json"]
    assert not hub.leaked_token, "the token is not forwarded to the host GitHub redirects the download to"

    # a second laptop, empty, gets the code from the release and the data from repo-data/
    other_launcher = device_with(repo, tmp_path / "laptop3")
    other_home = tmp_path / "home3"
    second = launch(other_launcher, other_home, "--smoke", hub=hub, name="laptop3")
    assert "smoke: serving" in second.stdout, second.stdout + second.stderr
    seen = json.loads((other_home / "data" / "devices.json").read_text())
    assert {"laptop2", "laptop3"} <= set(seen), "each device sees the others"

    # a release carrying a newer launcher, and a renamed app
    hub.add_release("v0.2", build(repo, "v0.2", launcher_version="9.9.9", app_name="Renamed"))
    before = launcher.read_text(encoding="utf-8")
    third = launch(launcher, home, "--smoke", hub=hub, name="laptop2")
    assert "smoke: serving" in third.stdout, third.stdout + third.stderr
    after = launcher.read_text(encoding="utf-8")
    assert 'LAUNCHER_VERSION = "9.9.9"' in after and after != before, "the launcher replaced itself"
    assert f'APP_NAME = "{SIGN}"' in after, "keeping this device's identity, so its data folder does not move"
    assert list(launcher.parent.glob("launcher.py.*.bak")), "the previous launcher is kept"
    assert installed(home)["tag"] == "v0.2" and installed(home)["previous"] == "v0.1"
    assert (home / "data" / "devices.json").exists(), "an update never touches the data folder"

    # forwards only, unless told otherwise
    back = launch(launcher, home, "--rollback", "--check", hub=hub, name="laptop2")
    assert "installed v0.1" in back.stdout, back.stdout


def test_pin_holds_a_version_through_a_newer_release(repo, hub, tmp_path):
    hub.add_release("v0.1", build(repo, "v0.1"))
    launcher = device_with(repo, tmp_path / "dev")
    home = tmp_path / "home"
    launch(launcher, home, "--smoke", hub=hub)
    hub.add_release("v0.2", build(repo, "v0.2"))
    launch(launcher, home, "--pin", "v0.1", "--smoke", hub=hub)
    assert (home / "versions" / "v0.1").is_dir(), "the pinned version survives the prune"
    run = launch(launcher, home, "--smoke", hub=hub)
    assert "running v0.1 (pinned)" in run.stdout, run.stdout
    launch(launcher, home, "--pin", "off", "--smoke", hub=hub)
    assert "running v0.2" in launch(launcher, home, "--smoke", hub=hub).stdout


# --------------------------------------------------------------------------------- 3. a zip, on a laptop
def test_zip_transport_takes_the_newest_zip_out_of_downloads(repo, tmp_path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / f"{SLUG}-v0.1.zip").write_bytes(build(repo, "v0.1"))
    (downloads / f"{SLUG}-v0.3.zip").write_bytes(build(repo, "v0.3"))
    launcher = device_with(repo, tmp_path / "laptop1")
    home = tmp_path / "home1"

    run = launch(launcher, home, "--source", "zip", "--downloads", str(downloads), "--smoke",
                 name="laptop1", token="")
    assert "smoke: serving" in run.stdout, run.stdout + run.stderr
    assert installed(home)["tag"] == "v0.3", "the newest zip wins, not the first one found"
    assert installed(home)["source"] == "zip"
    assert "not pushing" in run.stdout, "no token, no repository: nothing leaves this laptop"
    (home / "data" / "mine.txt").write_text("keep me", encoding="utf-8")

    # an older zip appears: it does not go backwards
    again = launch(launcher, home, "--source", "zip", "--downloads", str(downloads), "--smoke",
                   name="laptop1", token="")
    assert installed(home)["tag"] == "v0.3" and "found v0.3" in again.stdout

    # a zip that is not this app's is not this app's, whatever it is called
    with zipfile.ZipFile(downloads / f"{SLUG}-v9.8.zip", "w") as z:
        z.writestr("VERSION", "v9.8\n")
        z.writestr("app.py", "print('x')")
        z.writestr("appinfo.json", json.dumps({"name": "Other", "slug": "other", "port": 1, "entry": "app.py"}))
    foreign = launch(launcher, home, "--source", "zip", "--downloads", str(downloads), "--check",
                     name="laptop1", token="")
    assert f"not a {SIGN} release zip" in foreign.stdout, foreign.stdout
    assert "available v0.3" in foreign.stdout, "it stays on its own version"

    # one that is this app's, but broken, and reaching where it should not
    with zipfile.ZipFile(downloads / f"{SLUG}-v9.9.zip", "w") as z:
        z.writestr("VERSION", "v9.9\n")
        z.writestr("app.py", "raise SystemExit(1)")
        z.writestr("appinfo.json", json.dumps({**INFO, "port": 1}))
        z.writestr("data/mine.txt", "WIPED")
        z.writestr("../escaped.txt", "no")
    broken = launch(launcher, home, "--source", "zip", "--downloads", str(downloads), "--smoke",
                    name="laptop1", token="")
    assert (home / "data" / "mine.txt").read_text() == "keep me", "a zip never writes into the data folder"
    assert not (tmp_path / "escaped.txt").exists(), "and never outside its own folder"
    assert not (home / "versions" / "v9.9" / "data").exists()
    assert "did not start; falling back to v0.3" in broken.stdout, broken.stdout
    assert "smoke: serving" in broken.stdout, "the laptop is still working when the release is not"
    assert installed(home)["tag"] == "v0.3"

    # and it does not walk back into it on the next double-click
    after = launch(launcher, home, "--source", "zip", "--downloads", str(downloads), "--smoke",
                   name="laptop1", token="")
    assert "did not start last time" in after.stdout and "smoke: serving" in after.stdout, after.stdout


def test_a_zip_unpacked_by_hand_bootstraps_itself(repo, tmp_path):
    """The first time on a machine with no GitHub: unzip it anywhere, double-click Start-Offline."""
    unpacked = tmp_path / "unpacked"
    unpacked.mkdir()
    with zipfile.ZipFile(io.BytesIO(build(repo, "v0.4"))) as z:
        z.extractall(unpacked)
    assert (unpacked / "Start-Offline.bat").exists() and (unpacked / "launcher.py").exists()
    home = tmp_path / "home"
    run = launch(unpacked / "launcher.py", home, "--source", "zip", "--downloads", str(tmp_path / "none"),
                 "--smoke", name="laptop1", token="")
    assert "smoke: serving" in run.stdout, run.stdout + run.stderr
    assert installed(home)["tag"] == "v0.4" and installed(home)["source"] == "folder"


def test_the_zip_a_release_uploads_is_the_zip_a_laptop_installs(repo, hub, tmp_path):
    """One artifact, two transports: the same bytes serve the GitHub laptop and the offline one."""
    blob = build(repo, "v0.5")
    hub.add_release("v0.5", blob)
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / f"{SLUG}-v0.5.zip").write_bytes(blob)

    online = launch(device_with(repo, tmp_path / "a"), tmp_path / "home-a", "--smoke", hub=hub, name="online")
    offline = launch(device_with(repo, tmp_path / "b"), tmp_path / "home-b", "--source", "zip",
                     "--downloads", str(downloads), "--smoke", name="offline", token="")
    assert "smoke: serving" in online.stdout and "smoke: serving" in offline.stdout
    a = {p: (tmp_path / "home-a" / "versions" / "v0.5" / p).read_bytes()
         for p in ("app.py", "datasync.py", "launcher.py", "appinfo.json")}
    b = {p: (tmp_path / "home-b" / "versions" / "v0.5" / p).read_bytes() for p in a}
    assert a == b, "both laptops end up running byte-identical code"


# --------------------------------------------------------------------------------- 4. the phone
def test_ios_transport_runs_in_process_and_syncs(repo, hub, tmp_path):
    """Pythonista has no subprocesses: the app runs inside the launcher, and the web view is the window."""
    hub.add_release("v0.1", build(repo, "v0.1"))
    launcher = device_with(repo, tmp_path / "phone")
    home = tmp_path / "phone-home"

    run = launch(launcher, home, "--smoke", hub=hub, name="iphone", ios=True)
    assert "smoke: serving" in run.stdout, run.stdout + run.stderr
    assert installed(home)["tag"] == "v0.1"
    assert b"iphone" in hub.contents["repo-data/devices.json"], "the phone pushes what it did"
    seen = json.loads((home / "data" / "devices.json").read_text())
    assert seen["iphone"]["transport"] == "ios"

    # the phone updates itself too, in-process, with no exec
    hub.add_release("v0.2", build(repo, "v0.2", launcher_version="9.9.9"))
    second = launch(launcher, home, "--smoke", hub=hub, name="iphone", ios=True)
    assert "smoke: serving" in second.stdout, second.stdout + second.stderr
    assert 'LAUNCHER_VERSION = "9.9.9"' in launcher.read_text(encoding="utf-8")
    assert installed(home)["tag"] == "v0.2"


def test_a_laptop_and_the_phone_share_one_data_folder(repo, hub, tmp_path):
    hub.add_release("v0.1", build(repo, "v0.1"))
    launch(device_with(repo, tmp_path / "l"), tmp_path / "hl", "--smoke", hub=hub, name="laptop2")
    launch(device_with(repo, tmp_path / "p"), tmp_path / "hp", "--smoke", hub=hub, name="iphone", ios=True)
    back = launch(device_with(repo, tmp_path / "l2"), tmp_path / "hl2", "--smoke", hub=hub, name="laptop2b")
    assert "smoke: serving" in back.stdout, back.stdout
    seen = json.loads((tmp_path / "hl2" / "data" / "devices.json").read_text())
    assert {"laptop2", "iphone", "laptop2b"} <= set(seen)
    assert seen["iphone"]["transport"] == "ios" and seen["laptop2"]["transport"] == "github"


# --------------------------------------------------------------------------------- 5. nothing at all
def test_a_device_with_nothing_says_so_instead_of_crashing(repo, tmp_path):
    run = launch(device_with(repo, tmp_path / "bare"), tmp_path / "bare-home", "--offline", "--smoke",
                 name="bare", token="")
    assert run.returncode == 1 and "nothing installed" in run.stdout, run.stdout + run.stderr


# --------------------------------------------------------------------------------- 6. the app on top of it
def test_routes_added_to_the_app_are_actually_reachable(repo, tmp_path):
    """The trap this catches: a route written below the __main__ guard never registers, and the app answers
    404 with no error anywhere. Anything appended to app.py has to go above that line."""
    app_py = repo / "app" / "app.py"
    app_py.write_text(app_py.read_text(encoding="utf-8").replace(
        "# ---------------------------------------------------------------------------------------------------------\n"
        "# Routes go ABOVE",
        '@frame.route("GET", "/api/example")\n'
        'def example(request):\n'
        '    return {"ok": True, "where": frame.DATA}\n\n\n'
        '@frame.route("POST", "/api/example")\n'
        'def add_example(request):\n'
        '    if not (request.json.get("name") or "").strip():\n'
        '        return 400, {"error": "a name is needed"}\n'
        '    return {"added": request.json["name"]}\n\n\n'
        "# ---------------------------------------------------------------------------------------------------------\n"
        "# Routes go ABOVE", 1), encoding="utf-8")
    port = json.loads((repo / "app" / "appinfo.json").read_text())["port"]
    data = tmp_path / "data"
    proc = subprocess.Popen([PY, "app.py"], cwd=repo / "app",
                            env={**os.environ, "APP_DATA_DIR": str(data), "DEVICE_NAME": "dev"})
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except Exception:                                  # noqa: BLE001
                time.sleep(0.25)
        else:
            raise AssertionError("the app never answered")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/example", timeout=5) as r:
            assert json.load(r)["where"] == str(data), "an app route sees the shared data folder"
        post = urllib.request.Request(f"http://127.0.0.1:{port}/api/example", method="POST",
                                      data=b'{"name":"goblin"}', headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(post, timeout=5) as r:
            assert json.load(r)["added"] == "goblin"
        try:
            urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/api/example",
                                                          method="POST", data=b"{}"), timeout=5)
            raise AssertionError("a handler returning (400, ...) should be a 400")
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        proc.terminate()


def test_no_route_is_stranded_below_the_main_guard(repo):
    """The same trap, checked by reading: this is what fails the build for a derived app."""
    text = (repo / "app" / "app.py").read_text(encoding="utf-8")
    guard = text.find('if __name__ == "__main__":')
    assert guard > 0, "app.py keeps its __main__ guard"
    assert "@frame.route" not in text[guard:], "a route below the guard never registers"


def test_a_release_with_nothing_attached_still_installs_and_runs(repo, hub, tmp_path):
    """Attaching the zip needs uploads.github.com, which is a different host from the API and is not always
    reachable. Without it GitHub serves the repository archive instead — app/ and all — and that has to work."""
    hub.add_release("v0.1", b"", source=zipball(repo, "v0.1"), asset=False)
    home = tmp_path / "home"
    run = launch(device_with(repo, tmp_path / "laptop"), home, "--smoke", hub=hub, name="laptop2")
    assert "smoke: serving" in run.stdout, run.stdout + run.stderr
    installed_dir = home / "versions" / "v0.1"
    assert (installed_dir / "app.py").exists(), "the app folder is flattened to the top, as in a release zip"
    assert (installed_dir / "appinfo.json").exists() and (installed_dir / "launcher.py").exists()
    assert not (installed_dir / "app").exists(), "and not left nested"
    assert (home / "data" / "devices.json").exists()


def test_the_first_run_asks_for_the_token_and_says_when_it_is_wrong(repo, hub, tmp_path):
    """What Korbi sees the very first time he double-clicks Start.bat on a laptop with nothing on it."""
    hub.add_release("v0.1", build(repo, "v0.1"))
    home = tmp_path / "home"
    launcher = device_with(repo, tmp_path / "laptop")
    run = launch(launcher, home, "--smoke", hub=hub, name="laptop2", token="",
                 typed="not-a-real-token\nt0k3n\n")
    assert "needs your GitHub token to download" in run.stdout, run.stdout
    assert "that token did not work" in run.stdout, "a bad paste is said out loud, not swallowed"
    assert "token accepted" in run.stdout and "smoke: serving" in run.stdout, run.stdout
    assert (home / "token.txt").read_text().strip() == "t0k3n"

    # and never again on that device
    again = launch(launcher, home, "--smoke", hub=hub, name="laptop2", token="")
    assert "needs your GitHub token" not in again.stdout and "smoke: serving" in again.stdout


def test_the_offline_launcher_never_asks_for_anything(repo, tmp_path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / f"{SLUG}-v0.1.zip").write_bytes(build(repo, "v0.1"))
    run = launch(device_with(repo, tmp_path / "laptop1"), tmp_path / "home", "--source", "zip",
                 "--downloads", str(downloads), "--smoke", name="laptop1", token="", typed="")
    assert "GitHub token for" not in run.stdout, "it never prompts on the offline path"
    assert "needs your GitHub token" not in run.stdout, run.stdout
    assert "smoke: serving" in run.stdout, run.stdout


def test_a_rename_reaches_every_file_that_needs_the_identity(repo):
    """The online wrappers have to know the repository before there is any code on the machine to read it
    from, so publish stamps them as well as the launcher. If that breaks, a fresh laptop asks for a token and
    then fetches from the wrong repository — which looks like a bad token and is not one."""
    info = json.loads((repo / "app" / "appinfo.json").read_text())
    info.update(name="Renamed", slug="renamed", repo="me/Renamed")
    (repo / "app" / "appinfo.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    subprocess.run([PY, "-c", "import sys; sys.path.insert(0, '.'); import publish; publish.stamp_launcher()"],
                   cwd=repo, check=True, text=True, capture_output=True)
    launcher = (repo / "launcher.py").read_text(encoding="utf-8")
    assert 'DEFAULT_REPO = "me/Renamed"' in launcher and 'APP_NAME = "Renamed"' in launcher
    assert 'set "APP_REPO=me/Renamed"' in (repo / "Start.bat").read_text(encoding="utf-8")
    assert 'APP_REPO="me/Renamed"' in (repo / "Start.command").read_text(encoding="utf-8")
    assert "you/Basis" not in launcher, "no trace of the skeleton's own repository is left behind"


def test_the_first_run_leaves_an_icon_on_the_desktop(repo, hub, tmp_path):
    """What he asked for: after the first run, starting it again is one click on the desktop, and that click
    goes through the same wrapper — so it updates the launcher, the app and the data every time."""
    hub.add_release("v0.1", build(repo, "v0.1"))
    home = tmp_path / "home"
    launcher = device_with(repo, tmp_path / "laptop")
    run = launch(launcher, home, "--smoke", hub=hub, name="laptop2")
    assert "on the desktop" in run.stdout, run.stdout

    entry = tmp_path / "Desktop" / f"{SLUG}.desktop"                       # this machine is Linux
    assert entry.exists(), sorted(p.name for p in (tmp_path / "Desktop").iterdir())
    text = entry.read_text(encoding="utf-8")
    assert str(tmp_path / "laptop" / "Start.command") in text, "it points at the wrapper, not at one version"
    assert str(home / "icon.png") in text and (home / "icon.png").exists(), "and carries the app's icon"
    assert (tmp_path / "laptop" / "Start.command").exists(), "the wrappers land beside the launcher"
    assert (tmp_path / "laptop" / "Start-Offline.bat").exists(), "including the offline one, for later"

    # deleting it is a decision, not something to undo on the next start
    entry.unlink()
    again = launch(launcher, home, "--smoke", hub=hub, name="laptop2")
    assert not entry.exists() and "on the desktop" not in again.stdout


def test_the_offline_run_makes_its_own_icon(repo, tmp_path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / f"{SLUG}-v0.1.zip").write_bytes(build(repo, "v0.1"))
    home = tmp_path / "home"
    run = launch(device_with(repo, tmp_path / "laptop1"), home, "--source", "zip",
                 "--downloads", str(downloads), "--smoke", name="laptop1", token="")
    entry = tmp_path / "Desktop" / f"{SLUG}-offline.desktop"
    assert entry.exists(), run.stdout
    assert "Start-Offline.command" in entry.read_text(encoding="utf-8"), "the offline icon runs the zip path"
