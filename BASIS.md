# Basis — instructions for the assistant building on it

You have been handed `basis.zip` and asked to build a specific application on top of it. Read this first.
It tells you what Basis is, what you must not break, and exactly what to hand back.

Korbi is the person you are building for. Two things about him govern everything below:

* **He never uses a command line.** Not once, not for setup, not to recover from a mistake. Every action he
  takes is a double-click, a tap, or a click in a browser. If your instructions to him contain a terminal
  command, you have failed the brief. You may run commands yourself if you have a shell; he may not.
* He is technically fluent and terse. Write to him as an engineer, not a beginner.

---

## What Basis is

A skeleton with no features and a working delivery system around it. One application, three kinds of device,
one shared data folder. Your job is to put an application inside it — not to redesign the delivery.

| Device | What he double-clicks / taps | How code arrives |
|---|---|---|
| Laptop with GitHub | `Start.bat` | newest GitHub release |
| Laptop without GitHub | `Start-Offline.bat` | newest `<slug>-vN.zip` in Downloads |
| iPhone | a Shortcut → Pythonista | newest GitHub release |

All three run the same `launcher.py`. It replaces itself, installs the code, pulls the data, runs the app,
and pushes the data back when the window closes.

    launcher.py            what every device runs. Self-updating. DO NOT EDIT.
    Start*.bat/.command    the double-click files. DO NOT EDIT.
    Publish.bat/.command   cuts a version from a window.
    app/
      frame.py             the HTTP frame the launchers depend on. DO NOT EDIT.
      datasync.py          the data sync. DO NOT EDIT.
      app.py               ← the application. THIS IS YOURS.
      appinfo.json         ← name, slug, repo, port. THIS IS YOURS.
      templates/, static/  ← the page. THESE ARE YOURS.
      VERSION              written by publish.
    make_zip.py            builds the release artifact.
    publish.py             tag, push, release, upload.
    test_system.py         12 tests over all three transports. Keep them green.

---

## Ask before you build

1. The application's **name** (e.g. SpritesAPI) and one line on what it does.
2. Its **port** — anything unused, 5000–5999. Kontor and other Basis apps must not collide.
3. The **GitHub repository** it will live in (`owner/Name`), private is fine and normal.

Do not guess these. Everything downstream is stamped from them.

---

## About the token

Korbi creates a **fine-grained token** on GitHub, scoped to that one repository, with **Contents: Read and
write** (GitHub adds Metadata: Read-only itself). One token covers everything below. He gives it to you in the
conversation.

**You publish directly.** Your sandbox can reach `github.com` and `api.github.com`, so you create the
repository, push the code, and cut the release yourself. Do that rather than writing him a list of clicks —
the whole point is that he does none of this by hand.

One host is missing from the sandbox's allowlist: **`uploads.github.com`**, which is where a release's
attached zip goes. `publish.py` tries the upload, and when it is refused it says so and carries on, because
the release is already made and a device with nothing attached falls back to GitHub's source archive — which
the launcher flattens into the same layout. So a release published from a sandbox works; it just has no zip
hanging off it. Two consequences to pass on:

* The **offline laptop's zip** must come from you as a file, not from the release page. `publish.py` always
  writes it to Downloads, and it is a deliverable in your handoff either way.
* From **Claude Code on his own machine**, the upload is not blocked and the release gets its zip normally.

Which devices ask him for the token, and when:

| | Asks for a token | Why |
|---|---|---|
| `Start.bat` (GitHub laptop) | first run only | reads the release, syncs data |
| `Start-Offline.bat` | never | it never speaks to GitHub |
| Pythonista | first run only | same as the GitHub laptop |

Read *and* write, on all three counts: a private repository will not release its code without the read, and
the data sync needs the write. The prompt is a dialog box on Windows and a console line in Pythonista. On the
phone, typing a 90-character token is unpleasant — dropping a `token.txt` into the launcher's home folder
does the same job.

Two things to tell him. A fine-grained token expires, and GitHub proposes 30 days by default: when it lapses
the online laptop quietly stops updating and the phone stops syncing, both carrying on with what they have.
And a token pasted into a conversation lives in that transcript, so a short expiry, one repository, and a
revoke when the work is done is the cheap version of caring about it.

---

## The order of work

**1. Rename.** Edit `app/appinfo.json` only:

```json
{ "name": "SpritesAPI", "slug": "spritesapi", "repo": "korbi/SpritesAPI", "port": 5031, "entry": "app.py" }
```

The slug is lowercase, no spaces, no punctuation — it is the zip filename prefix and the environment-variable
prefix. Then propagate it into the launcher by running `publish.stamp_launcher()` (or `python publish.py v0.1
--zip`, which stamps and builds in one go). Never hand-edit the identity block in `launcher.py`; it is
generated, and `publish` will overwrite your edits.

**2. Write the application** in `app/app.py`, and the page in `app/templates/index.html` +
`app/static/`. Nothing else. See the next section.

**3. Rewrite `README.md`** for the new app. Keep the structure; replace the Basis text.

**4. Keep the tests green.** `python -m pytest test_system.py -q` — 12 tests, ~20 seconds. They read the app's
identity from `appinfo.json`, so they survive the rename. If you add routes, add a test alongside the existing
`test_routes_added_to_the_app_are_actually_reachable`. **A red suite is not a deliverable.**

**5. Build the artifacts** and write the handoff (both below).

---

## Writing the application

`app.py` starts empty on purpose. Add routes with a decorator:

```python
import frame

@frame.route("GET", "/api/sprites")
def sprites(request):
    return {"sprites": read_sprites()}              # a dict or list is sent as JSON

@frame.route("POST", "/api/sprites")
def add(request):
    name = (request.json.get("name") or "").strip()
    if not name:
        return 400, {"error": "a name is needed"}   # (status, body) sets the status
    ...
    return {"ok": True}
```

A handler receives a `Request` (`.method`, `.path`, `.query`, `.json`, `.body`) and returns a dict or list
(sent as JSON), a `(status, dict)` pair, `frame.html(text)`, or `frame.raw(bytes, content_type)`.
Registering `("GET", "/")` replaces the shell page.

**Four rules, in order of how badly it goes when you break them:**

1. **Routes go above the `__main__` guard at the bottom of `app.py`.** Below it, they never register and the
   app answers 404 with no error anywhere. There is a banner in the file and a test that fails the build.
   Appending to the end of the file is the natural thing to do and it is wrong.
2. **The standard library only.** The offline laptop cannot reach PyPI and Pythonista has no pip. Anything you
   add to `requirements.txt` is a dependency two of the three devices will not have — the desktop-with-GitHub
   laptop will install it and the other two will crash. If you genuinely need a library, say so explicitly in
   the handoff and explain which devices lose.
3. **All state goes under `frame.DATA`.** Plain files, written atomically (write `.tmp`, then `os.replace`).
   Never write beside the code: `versions/<tag>/` is wiped on every update. Never write outside `frame.DATA`.
4. **Keep files independent.** The sync is newest-wins *per file*, not a merge. Two devices editing the same
   file in the same minute means one loses silently. Prefer one file per record (`data/sprites/goblin.json`)
   over one big file every device rewrites. This is the single most important design decision you will make
   in the app's data layout, and it is very hard to change later.

Not for storage: anything large or binary-heavy (every byte round-trips through the GitHub contents API on
every push), and anything secret — `token.txt` is excluded from the sync, nothing else is.

The page must work at 380 px. The phone is a first-class target, not an afterthought.

---

## Building the artifacts

Run these yourself. Korbi does not.

**Hand back three files, one per device.** Each is a single file; none of them needs the others.

1. **`Start.bat`** — the online laptop, and all it needs. On the first double-click it asks for the GitHub
   token, fetches `launcher.py` out of the repository with it, saves the token, and runs. After that the
   launcher updates itself, the app and the data, and the file never asks again. (`Start.command` is the same
   thing for macOS.) Hand this over on its own — the online laptop has no use for a zip of code it replaces
   on first run.
2. **`<slug>-v0.1.zip`** — the offline laptop, which cannot fetch anything. `python publish.py v0.1 --zip`
   builds it. He unzips it anywhere and double-clicks `Start-Offline.bat` inside. To update it later he drops
   a newer zip in Downloads.
3. **`launcher.py`** — the phone. It goes into **Pythonista 3**'s folder (App Store, one-off ~$10) and a
   Shortcut runs it. iOS has no double-clickable script, so the Shortcut is the executable.

   The phone is the only device that costs anything, and it is optional per app — the two laptops work
   without it. It needs an interpreter that runs Python *inside its own process* and can show a web view
   there: bounce out to Safari instead and iOS suspends the interpreter, killing the server mid-session.
   Pythonista does this with `ui.WebView`, which is what the launcher uses. **Pyto** (free, open source,
   Python 3.10, native Shortcuts actions) is the same architecture and is the one to try if he does not want
   to buy anything — the launcher detects it, but its web view is a different API and is not written yet, so
   it would fall back to Safari. **a-Shell** and **iSH** are terminals, which rules them out on his terms and
   on the suspension problem both. Do not tell him the phone works for free without saying which part is
   untested.

**Publishing.** `publish.py v0.1 "notes"` sets the version, stamps the launcher, commits, tags, pushes, and
creates the release. Do it yourself with his token. Do not claim a release exists if it does not.

**The desktop icon is already written too.** After the first run that gets as far as starting the app, the
launcher puts a shortcut on the desktop pointing at the wrapper — `Start.bat` online, `Start-Offline.bat`
offline — with the app's icon from `app/static/icon.ico`. Every click after that goes through the same path,
so it updates the launcher, the code and the data. It is made once; if he deletes it, that was a decision and
it is not put back. Replace `app/static/icon.ico` and `icon.png` with something that suits the app you build;
they are a generic mark.

**The first-run prompt is already written** — the launcher asks for the token before anything else, checks it
against the repository on the spot, and says so if it is wrong. Do not add your own prompt, and do not put a
token in any file you hand over.

---

## The handoff

End with this, filled in. Nothing else after it.

> **The one thing on GitHub** (in a browser, once)
>
> 1. Create a repository `<owner>/<Name>` — private.
> 2. Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token.
>    Repository access: **only this repository**. Permissions: **Contents → Read and write**. Nothing else.
>    Copy it here; it is shown once. Everything after that is mine — I push the code and cut the release.
>
> **Laptop with GitHub** — `Start.bat`
>
> Put it in a folder of its own (it downloads next to itself) and double-click it. It asks for the token
> once, fetches the app, and runs. Every start after that updates itself. Nothing else to install.
>
> **Laptop without GitHub** — `<slug>-v0.1.zip`
>
> Copy it across, unzip it anywhere, double-click **Start-Offline.bat** inside. To update later: put a newer
> zip in Downloads and double-click again. Nothing ever leaves that laptop.
>
> **iPhone** — `launcher.py`
>
> 1. Save it into the Pythonista 3 folder: Files app → wherever you put `launcher.py` → copy it into
>    *On My iPhone → Pythonista 3*.
> 2. Shortcuts → new shortcut → one **Open URL** action: `pythonista3://launcher.py?action=run` → add to
>    Home Screen. (If that URL does nothing, try `pythonista3://x-callback-url/run?script=launcher.py`.)
> 3. The first tap asks for the token in Pythonista's console; after that it is one tap and no typing.

Then tell him, in one line each: what the app does, where its data lives on each device, and anything you had
to compromise on.

---

## What is proven, and what is not

Tested for real, end to end, against a stub GitHub — the launcher finding a version, replacing itself and
restarting, installing, pulling data, running the app, pushing on close; the newest-zip-wins rule; a foreign
zip refused; a zip reaching into `data/` or `..` refused; a release that will not start falling back to the
previous one and being skipped afterwards; pin and rollback; three devices converging on one data folder;
the same bytes running on the online and offline laptops; and the device token not being forwarded to the
storage host GitHub redirects asset downloads to — which fails a private repository outright, and hands a
token to a third party if you get it wrong.

**Not tested:** anything inside Pythonista itself. The iOS path is exercised as pure Python (in-process run,
in-process self-update, sync), which is the whole of the logic, but `ui.WebView` presentation and the
`pythonista3://` URL scheme are written from documentation, not from a running phone. If the URL does not
work, `pythonista3://x-callback-url/run?script=launcher.py` is the documented alternative. Say this to Korbi
rather than letting him discover it.

**Known limits, worth repeating to him if they touch the app you built:** newest-wins per file, never a merge;
a file deleted locally is not deleted on GitHub; data on the offline laptop is a dead end — nothing carries it
back; every data file round-trips through the GitHub contents API, so this suits kilobytes, not megabytes.
