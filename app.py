import os
import re
import json
import zipfile
import io
import base64
import requests
import bcrypt
import threading
import uuid
import queue
import time
import tempfile
import subprocess
from flask import Flask, render_template, request, session, redirect, url_for, Response
from dotenv import load_dotenv

# ===================== Bootstrap =====================
load_dotenv()

app = Flask(__name__)

# Use a fixed secret if provided (recommended), else fallback to random (dev only)
app.secret_key = os.getenv("FLASK_SECRET_KEY", default=None) or os.urandom(32)

# ===================== Config =====================
# GitHub settings
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN_ENV")
GITHUB_REPO = os.getenv("GITHUB_REPO", "Team-Vegavath/Ignition1.0")  # allow override via env
if not GITHUB_TOKEN:
    raise RuntimeError("❌ GITHUB_TOKEN_ENV not found in environment or .env file")

print("Loaded GITHUB_TOKEN:", (GITHUB_TOKEN[:10] + "…") if GITHUB_TOKEN else "❌ None")
print("Target repo:", GITHUB_REPO)

# Upload policy
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "90"))  # keep below GH's ~100MB limit
SKIP_DIRS = {
    ".git", "venv", ".venv", "__pycache__", "node_modules",
    ".idea", ".vscode", "dist", "build", ".pytest_cache", ".mypy_cache"
}
SKIP_FILE_PATTERNS = {
    r"\.DS_Store$", r"Thumbs\.db$", r"desktop\.ini$"
}
# compile once
_SKIP_FILE_REGEXES = [re.compile(p, re.IGNORECASE) for p in SKIP_FILE_PATTERNS]


# ===================== Persistence =====================
# Load teams (hashed pins)
with open("teams.json", "r", encoding="utf-8") as f:
    teams = json.load(f)

# Upload tracking file
UPLOAD_STATE_FILE = "uploads_state.json"
if os.path.exists(UPLOAD_STATE_FILE):
    try:
        with open(UPLOAD_STATE_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            upload_state = json.loads(content) if content else {}
    except Exception:
        upload_state = {}
else:
    upload_state = {}

def save_upload_state():
    with open(UPLOAD_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(upload_state, f, indent=2, ensure_ascii=False)


# ===================== Helpers =====================
def normalize_path(p: str) -> str:
    """
    Normalize a repo path for GitHub:
      - convert backslashes to forward slashes
      - strip leading ./ or /
      - remove any .. path traversal
      - collapse duplicate slashes
    """
    p = p.replace("\\", "/")
    p = re.sub(r"(^\./|^/)+", "", p)
    parts = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            # drop traversal
            continue
        parts.append(seg)
    return "/".join(parts)

def should_skip(rel_path: str) -> bool:
    """
    Decide if a file should be skipped based on directory name or known junk patterns.
    """
    # fast dir check
    parts = rel_path.replace("\\", "/").split("/")
    if any(seg in SKIP_DIRS for seg in parts):
        return True
    # file patterns
    base = parts[-1] if parts else rel_path
    for rx in _SKIP_FILE_REGEXES:
        if rx.search(base):
            return True
    return False

def is_too_large(local_path: str) -> bool:
    try:
        return os.path.getsize(local_path) > MAX_FILE_MB * 1024 * 1024
    except FileNotFoundError:
        return True


# ===================== GitHub Upload Logic =====================
_SESSION = requests.Session()
_GH_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "Ignition-Uploader/1.0"
}

def _gh_request(method: str, url: str, **kw) -> requests.Response | None:
    """
    Do a GitHub API request with retries and timeouts.
    Retries on 429 and 5xx with exponential backoff.
    """
    timeout = kw.pop("timeout", 30)
    max_retries = 3
    backoff = 1.0

    for attempt in range(1, max_retries + 1):
        try:
            res = _SESSION.request(method, url, headers=_GH_HEADERS, timeout=timeout, **kw)
            if res.status_code in (429, 500, 502, 503, 504):
                if attempt < max_retries:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
            return res
        except requests.exceptions.RequestException:
            if attempt < max_retries:
                time.sleep(backoff)
                backoff *= 2
                continue
            return None
    return None

def github_upload_bytes(content_bytes: bytes, github_repo_path: str) -> requests.Response | None:
    """
    Upload bytes directly to GitHub via REST API (create/update a single file).
    """
    github_repo_path = normalize_path(github_repo_path)
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{github_repo_path}"

    # Check if file exists to include SHA
    get_res = _gh_request("GET", url)
    sha = None
    if get_res and get_res.status_code == 200:
        try:
            sha = get_res.json().get("sha")
        except Exception:
            sha = None

    payload = {
        "message": f"Upload {github_repo_path}",
        "content": base64.b64encode(content_bytes).decode("utf-8")
    }
    if sha:
        payload["sha"] = sha

    put_res = _gh_request("PUT", url, json=payload)
    return put_res


# ===================== Background Upload Thread =====================
upload_queues: dict[str, "queue.Queue[tuple[str, str]]"] = {}

def json_event(event, data):
    import json as _json
    return (event, _json.dumps(data, ensure_ascii=False))

def _upload_file_bytes(q: queue.Queue, data: bytes, github_path: str, idx: int, total: int):
    put_res = github_upload_bytes(data, github_path)
    status_code = getattr(put_res, "status_code", None)
    print("Uploading:", github_path, "→", status_code)

    if put_res and status_code in (200, 201):
        try:
            result = put_res.json()
            file_url = result.get("content", {}).get("html_url", "")
        except Exception:
            file_url = ""
        q.put(json_event("upload_done", {"file": github_path, "index": idx, "total": total}))
        q.put(json_event("github_log", {"file": github_path, "status": status_code, "url": file_url}))
    else:
        resp_text = ""
        try:
            resp_text = (put_res.text if put_res is not None else "No response")[:300]
        except Exception:
            pass
        q.put(json_event("upload_error", {"file": github_path, "status": status_code, "response": resp_text}))


def process_upload(upload_id: str, team: str, zip_bytes: bytes):
    """
    Extract ZIP in memory & upload directly to GitHub. Skips unwanted dirs/files.
    """
    q = upload_queues.get(upload_id)
    if not q:
        return

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            # sanitize + filter
            filtered = []
            skipped = 0
            for name in names:
                # normalize early for filtering
                norm = normalize_path(name)
                if should_skip(norm):
                    skipped += 1
                    continue
                try:
                    # size check using ZipInfo
                    info = z.getinfo(name)
                    if info.file_size > MAX_FILE_MB * 1024 * 1024:
                        skipped += 1
                        q.put(json_event("upload_error", {
                            "file": norm, "status": "skipped", "response": f"File too large (> {MAX_FILE_MB} MB)"
                        }))
                        continue
                except KeyError:
                    pass
                filtered.append((name, norm))

            total = len(filtered)
            q.put(json_event("info", f"{total} files to upload (skipped {skipped})."))

            for idx, (zip_name, rel_norm) in enumerate(filtered, start=1):
                data = z.read(zip_name)
                github_path = f"submissions/{team}/{rel_norm}"
                _upload_file_bytes(q, data, github_path, idx, total)
                time.sleep(0.03)

        q.put(json_event("finished", "✅ All uploads completed successfully!"))

    except Exception as e:
        q.put(json_event("error", f"Exception during upload: {repr(e)}"))
    finally:
        q.put(json_event("closed", "Upload thread ended."))


# ============ GitHub Repo Clone Upload ==============
_GH_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)(?:\.git)?(?:/)?$"
)

def process_github_clone(upload_id: str, team: str, repo_url: str):
    """
    Clone a public GitHub repo and upload its contents, skipping .git and other unnecessary folders.
    """
    q = upload_queues.get(upload_id)
    if not q:
        return

    try:
        repo_url = repo_url.strip()
        if not _GH_URL_RE.match(repo_url):
            q.put(json_event("error", "❌ Invalid GitHub URL. Expected format: https://github.com/<owner>/<repo>"))
            q.put(json_event("closed", "Upload thread ended."))
            return

        q.put(json_event("info", f"Cloning repository: {repo_url}"))

        with tempfile.TemporaryDirectory() as tmpdir:
            cmd = ["git", "clone", "--depth", "1", repo_url, tmpdir]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                q.put(json_event("error", f"❌ Git clone failed: {res.stderr.strip() or res.stdout.strip()}"))
                return

            q.put(json_event("info", "✅ Clone successful, preparing upload..."))

            total_files: list[str] = []
            skipped_files = 0

            for root, dirs, files in os.walk(tmpdir):
                # in-place prune of unwanted dirs
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]

                for fname in files:
                    file_path = os.path.join(root, fname)
                    rel_path = os.path.relpath(file_path, tmpdir)
                    rel_norm = normalize_path(rel_path)

                    if should_skip(rel_norm):
                        skipped_files += 1
                        continue

                    if is_too_large(file_path):
                        skipped_files += 1
                        q.put(json_event("upload_error", {
                            "file": rel_norm, "status": "skipped",
                            "response": f"File too large (> {MAX_FILE_MB} MB)"
                        }))
                        continue

                    total_files.append(file_path)

            q.put(json_event("info", f"Found {len(total_files)} files to upload (skipped {skipped_files})."))

            total = len(total_files)
            for idx, path in enumerate(total_files, start=1):
                rel_norm = normalize_path(os.path.relpath(path, tmpdir))
                github_path = f"submissions/{team}/{rel_norm}"

                with open(path, "rb") as f:
                    data = f.read()

                _upload_file_bytes(q, data, github_path, idx, total)
                time.sleep(0.02)

            q.put(json_event("finished", f"✅ Uploaded {total} files successfully (skipped {skipped_files})."))

    except Exception as e:
        q.put(json_event("error", f"Exception during GitHub clone: {repr(e)}"))
    finally:
        q.put(json_event("closed", "Upload thread ended."))


# ===================== Routes =====================
@app.route("/", methods=["GET", "POST"])
def verify():
    if request.method == "POST":
        team = request.form.get("team")
        pin = request.form.get("pin", "").encode()

        stored_hash = teams.get(team)
        if stored_hash and bcrypt.checkpw(pin, stored_hash.encode()):
            # disguised admin
            if team == "VEGAVATH ADS":
                session["admin"] = True
                return redirect(url_for("admin_panel"))

            # prevent multiple submissions
            if upload_state.get(team):
                return render_template("verify.html", teams=teams.keys(),
                                       error="⚠️ Your team already submitted. Contact admin to unlock.")

            session["team"] = team
            return redirect(url_for("upload"))

        return render_template("verify.html", teams=teams.keys(), error="❌ Wrong PIN")

    return render_template("verify.html", teams=teams.keys())


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if "team" not in session:
        return redirect(url_for("verify"))

    team = session["team"]

    if request.method == "POST":
        file = request.files.get("file")
        repo_url = (request.form.get("repo_url") or "").strip()

        if not file and not repo_url:
            return render_template("upload.html", team=team,
                                   error="Please upload a ZIP or provide a public GitHub repo link.")

        # Mark submitted (before starting thread) to block duplicates
        upload_state[team] = True
        save_upload_state()

        upload_id = str(uuid.uuid4())
        q = queue.Queue()
        upload_queues[upload_id] = q

        if repo_url:
            threading.Thread(
                target=process_github_clone,
                args=(upload_id, team, repo_url),
                daemon=True
            ).start()
        else:
            # Read ZIP into memory
            zip_bytes = file.read()
            threading.Thread(
                target=process_upload,
                args=(upload_id, team, zip_bytes),
                daemon=True
            ).start()

        # Clear session to avoid re-submission via back/refresh
        session.clear()
        return redirect(url_for("success", upload_id=upload_id))

    return render_template("upload.html", team=team)


@app.route("/success")
def success():
    upload_id = request.args.get("upload_id")
    return render_template("success.html", upload_id=upload_id)


@app.route("/events/<upload_id>")
def events(upload_id):
    if upload_id not in upload_queues:
        return "Invalid", 404

    q = upload_queues[upload_id]

    def stream():
        while True:
            event, data = q.get()
            yield f"event: {event}\ndata: {data}\n\n"
            if event == "closed":
                break

        # cleanup the queue after closed to avoid leak
        try:
            del upload_queues[upload_id]
        except KeyError:
            pass

    return Response(stream(), mimetype="text/event-stream")


@app.route("/admin")
def admin_panel():
    if "admin" not in session:
        return redirect(url_for("verify"))
    return render_template("admin.html", upload_state=upload_state)


@app.route("/admin/reset/<team>", methods=["POST"])
def reset_team(team):
    if "admin" not in session:
        return redirect(url_for("verify"))
    upload_state[team] = False
    save_upload_state()
    return redirect(url_for("admin_panel"))


# ===================== Start =====================
if __name__ == "__main__":
    # Note: for production use gunicorn/uvicorn instead of Flask dev server
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
