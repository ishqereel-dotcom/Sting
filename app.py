import os
import sys
import json
import uuid
import html
import shutil
import zipfile
import subprocess
import threading
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware


# ================= CONFIGURATION =================

APP_NAME = "ADII X HOST"

DATA_DIR = Path(os.getenv("DATA_DIR", "/tmp/adii-host"))
BOTS_DIR = DATA_DIR / "bots"
LOGS_DIR = DATA_DIR / "logs"
DB_FILE = DATA_DIR / "bots.json"

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
APP_SECRET = os.getenv("APP_SECRET", "")

for folder in (DATA_DIR, BOTS_DIR, LOGS_DIR):
    folder.mkdir(parents=True, exist_ok=True)

app = FastAPI(title=APP_NAME)

if not APP_SECRET:
    raise RuntimeError("Missing APP_SECRET environment variable")

if not ADMIN_USERNAME or not ADMIN_PASSWORD:
    raise RuntimeError(
        "Set ADMIN_USERNAME and ADMIN_PASSWORD in Render Environment."
    )

app.add_middleware(
    SessionMiddleware,
    secret_key=APP_SECRET,
    https_only=True,
    same_site="lax",
    max_age=3600,
)

lock = threading.RLock()
processes = {}


# ================= DATABASE =================

def load_data():
    if not DB_FILE.exists():
        return {}

    try:
        result = json.loads(DB_FILE.read_text(encoding="utf-8"))
        return result if isinstance(result, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


bots = load_data()


def save_data():
    temp_file = DB_FILE.with_suffix(".tmp")
    temp_file.write_text(
        json.dumps(bots, indent=2),
        encoding="utf-8",
    )
    temp_file.replace(DB_FILE)


def logged_in(request):
    return request.session.get("logged_in") is True


def valid_bot(bot_id):
    return (
        isinstance(bot_id, str)
        and bot_id in bots
        and bot_id.isalnum()
    )


def get_status(bot_id):
    process = processes.get(bot_id)

    if process is not None and process.poll() is None:
        return "RUNNING"

    return "STOPPED"


def get_logs(bot_id):
    log_file = LOGS_DIR / f"{bot_id}.log"

    if not log_file.exists():
        return "No logs available."

    try:
        with log_file.open("rb") as file:
            file.seek(0, os.SEEK_END)
            size = file.tell()
            file.seek(max(0, size - 10000))
            return file.read().decode("utf-8", errors="replace")
    except OSError:
        return "Unable to read logs."


# ================= HTML =================

def layout(title, body):
    return HTMLResponse(
        f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
*{{box-sizing:border-box}}
body{{background:#10131c;color:#f3f4f6;font-family:Arial;
max-width:800px;margin:25px auto;padding:16px}}
.card{{background:#1d2332;padding:18px;margin:15px 0;
border-radius:12px;overflow-wrap:anywhere}}
input,button{{padding:12px;margin:7px 0;max-width:100%;
border-radius:7px}}
input[type=text],input[type=password]{{width:100%}}
button{{background:#16a34a;color:white;border:0;font-weight:bold}}
.danger{{background:#dc2626}}
a{{color:#60a5fa}}
pre{{background:#080a10;padding:12px;white-space:pre-wrap;
overflow-wrap:anywhere}}
</style>
</head>
<body>
<h1>🚀 ADII X HOST</h1>
<p>Personal Telegram Bot Manager</p>
{body}
</body>
</html>"""
    )


def require_login(request):
    if not logged_in(request):
        return RedirectResponse("/login", status_code=303)
    return None


# ================= HEALTH CHECK =================

@app.get("/health")
def health():
    return {"status": "ok", "app": APP_NAME}


# ================= LOGIN =================

@app.get("/login")
def login_page(request: Request):
    if logged_in(request):
        return RedirectResponse("/", status_code=303)

    return layout(
        "Admin Login",
        """
        <div class="card">
        <h2>🔐 Admin Login</h2>
        <form method="post" action="/login">
            <input name="username" placeholder="Username" required>
            <input name="password" type="password"
                   placeholder="Password" required>
            <button type="submit">Login</button>
        </form>
        </div>
        """,
    )


@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if (
        username == ADMIN_USERNAME
        and password == ADMIN_PASSWORD
    ):
        request.session.clear()
        request.session["logged_in"] = True
        return RedirectResponse("/", status_code=303)

    return HTMLResponse("Invalid username or password.", status_code=401)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ================= DASHBOARD =================

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    redirect = require_login(request)
    if redirect:
        return redirect

    cards = ""

    with lock:
        bot_items = list(bots.items())

    for bot_id, info in bot_items:
        name = html.escape(str(info.get("name", bot_id)))
        status = get_status(bot_id)

        cards += f"""
        <div class="card">
            <h3>🤖 {name}</h3>
            <p>ID: {bot_id}</p>
            <p>Status: <b>{status}</b></p>

            <form method="post" action="/start/{bot_id}">
                <button>▶ Start</button>
            </form>

            <form method="post" action="/stop/{bot_id}">
                <button class="danger">⏹ Stop</button>
            </form>

            <a href="/logs/{bot_id}">📋 View Logs</a>

            <form method="post" action="/delete/{bot_id}"
                  onsubmit="return confirm('Delete this bot?')">
                <button class="danger">🗑 Delete</button>
            </form>
        </div>
        """

    if not cards:
        cards = '<div class="card">No bots uploaded yet.</div>'

    body = f"""
    <div class="card">
        <h2>📤 Upload Python Bot</h2>
        <form method="post" action="/upload"
              enctype="multipart/form-data">
            <input type="file" name="file"
                   accept=".py,.zip" required>
            <button type="submit">Upload Bot</button>
        </form>
        <p>Supported: .py and .zip (maximum 20 MB).</p>
    </div>

    <div class="card">
        <h3>🔑 Bot Token</h3>
        <p>Configure BOT_TOKEN in Render Environment Variables.
        Never publish your token on GitHub.</p>
    </div>

    <h2>📦 Your Bots</h2>
    {cards}
    <p><a href="/logout">Logout</a></p>
    """

    return layout(APP_NAME, body)


# ================= UPLOAD =================

@app.post("/upload")
async def upload_bot(request: Request, file: UploadFile = File(...)):
    redirect = require_login(request)
    if redirect:
        return redirect

    filename = Path(file.filename or "").name

    if not filename.lower().endswith((".py", ".zip")):
        return HTMLResponse("Upload a .py or .zip file.", status_code=400)

    bot_id = uuid.uuid4().hex[:12]
    bot_dir = BOTS_DIR / bot_id
    bot_dir.mkdir(parents=True)

    uploaded_file = bot_dir / filename
    size = 0

    try:
        with uploaded_file.open("wb") as output:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break

                size += len(chunk)
                if size > 20 * 1024 * 1024:
                    raise ValueError("Upload limit is 20 MB.")

                output.write(chunk)

        if filename.lower().endswith(".zip"):
            source_dir = bot_dir / "source"
            source_dir.mkdir()

            if not zipfile.is_zipfile(uploaded_file):
                raise ValueError("Invalid ZIP file.")

            with zipfile.ZipFile(uploaded_file) as archive:
                root = source_dir.resolve()
                members = archive.infolist()

                if len(members) > 500:
                    raise ValueError("ZIP contains too many files.")

                total = 0
                for member in members:
                    target = (source_dir / member.filename).resolve()

                    if not target.is_relative_to(root):
                        raise ValueError("Unsafe ZIP path.")

                    total += member.file_size
                    if total > 100 * 1024 * 1024:
                        raise ValueError("Extracted ZIP exceeds 100 MB.")

                archive.extractall(source_dir)

            uploaded_file.unlink(missing_ok=True)

            preferred = {"main.py", "bot.py", "app.py", "run.py"}
            candidates = [
                path for path in source_dir.rglob("*.py")
                if "__pycache__" not in path.parts
            ]

            if not candidates:
                raise ValueError("ZIP does not contain a Python file.")

            candidates.sort(
                key=lambda path: (
                    path.name.lower() not in preferred,
                    len(path.parts),
                )
            )
            entry = candidates[0]

        else:
            entry = uploaded_file

        with lock:
            bots[bot_id] = {
                "name": filename,
                "directory": str(bot_dir),
                "entry": str(entry.relative_to(bot_dir)),
            }
            save_data()

        return RedirectResponse("/", status_code=303)

    except Exception as exc:
        shutil.rmtree(bot_dir, ignore_errors=True)
        return HTMLResponse(
            "Upload failed: " + html.escape(str(exc)),
            status_code=400,
        )

    finally:
        await file.close()


# ================= START BOT =================

@app.post("/start/{bot_id}")
def start_bot(request: Request, bot_id: str):
    redirect = require_login(request)
    if redirect:
        return redirect

    with lock:
        if not valid_bot(bot_id):
            return HTMLResponse("Bot not found.", status_code=404)

        current = processes.get(bot_id)
        if current and current.poll() is None:
            return RedirectResponse("/", status_code=303)

        info = bots[bot_id]
        bot_dir = Path(info["directory"]).resolve()
        entry = (bot_dir / info["entry"]).resolve()

        if
