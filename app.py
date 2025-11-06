import os
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
from flask import Flask, render_template, request, session, redirect, url_for, jsonify, Response
from dotenv import load_dotenv

# Load .env (for GitHub token)
load_dotenv()
app = Flask(__name__)
app.secret_key = os.urandom(32)

# ===================== Config =====================

# GitHub Settings
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN_ENV")
GITHUB_REPO = "Manu-Raj/TestSubmissions"  # Format: username/repo

print("Loaded GITHUB_TOKEN:", GITHUB_TOKEN[:10] if GITHUB_TOKEN else "❌ None")

if not GITHUB_TOKEN:
    raise RuntimeError("❌ GITHUB_TOKEN not found in environment or .env file")

# Load teams (hashed pins)
with open("teams.json", "r") as f:
    teams = json.load(f)

# Upload tracking file
UPLOAD_STATE_FILE = "uploads_state.json"
if os.path.exists(UPLOAD_STATE_FILE):
    try:
        with open(UPLOAD_STATE_FILE, "r") as f:
            content = f.read().strip()
            upload_state = json.loads(content) if content else {}
    except:
        upload_state = {}
else:
    upload_state = {}

def save_upload_state():
    with open(UPLOAD_STATE_FILE, "w") as f:
        json.dump(upload_state, f, indent=2)

# ================ GitHub Upload Logic ==================

def github_upload_bytes(content_bytes, github_repo_path):
    """Upload bytes directly to GitHub via REST API."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{github_repo_path}"

    content = base64.b64encode(content_bytes).decode()
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    get_res = requests.get(url, headers=headers)
    sha = get_res.json().get("sha") if get_res.status_code == 200 else None

    payload = {"message": f"Upload {github_repo_path}", "content": content}
    if sha:
        payload["sha"] = sha

    put_res = requests.put(url, json=payload, headers=headers)
    return put_res


# ================ Background Upload Thread ==================

upload_queues = {}

def json_event(event, data):
    import json as _json
    return (event, _json.dumps(data))

def process_upload(upload_id, team, zip_bytes):
    """Extract ZIP in memory & upload directly to GitHub."""
    q = upload_queues.get(upload_id)
    if not q:
        return

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            all_files = [f for f in z.namelist() if not f.endswith("/")]

            total = len(all_files)
            q.put(json_event("info", f"{total} files to upload."))

            for idx, filename in enumerate(all_files, start=1):
                data = z.read(filename)
                github_path = f"submissions/{team}/{filename}"

                put_res = github_upload_bytes(data, github_path)
                print("Uploading:", github_path, "→", put_res.status_code)

                if put_res.status_code in (200, 201):
                    result = put_res.json()
                    file_url = result.get("content", {}).get("html_url", "")
                    q.put(json_event("upload_done", {"file": github_path, "index": idx, "total": total}))
                    q.put(json_event("github_log", {
                        "file": github_path,
                        "status": put_res.status_code,
                        "url": file_url
                    }))
                else:
                    q.put(json_event("upload_error", {
                        "file": github_path,
                        "status": put_res.status_code,
                        "response": put_res.text[:200]
                    }))

                time.sleep(0.1)

        q.put(json_event("finished", "✅ All uploads completed successfully!"))

    except Exception as e:
        q.put(json_event("error", f"Exception during upload: {repr(e)}"))
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
        file = request.files["file"]
        if not file:
            return render_template("upload.html", team=team, error="No file selected")

        # Read ZIP into memory
        zip_bytes = file.read()

        # Mark submitted
        upload_state[team] = True
        save_upload_state()

        upload_id = str(uuid.uuid4())
        q = queue.Queue()
        upload_queues[upload_id] = q

        threading.Thread(target=process_upload, args=(upload_id, team, zip_bytes), daemon=True).start()

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

    return Response(stream(), mimetype="text/event-stream")

# ===================== Admin Panel =====================

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
    app.run(host="0.0.0.0", port=5000)
