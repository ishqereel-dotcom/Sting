import os
import sys
import json
import uuid
import shutil
import zipfile
import subprocess
import threading
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

# ================= CONFIG =================

APP_NAME = "ADII X HOST"
BASE_DIR = Path(os.getenv("DATA_DIR", "/tmp/adii-host"))
BOTS_DIR = BASE_DIR / "bots"
LOGS_DIR = BASE_DIR / "logs"
CONFIG_FILE = BASE_DIR / "bots.json"

ADMIN_USER = os.getenv("ADMIN_USERNAME", "")
ADMIN_PASS = os.getenv("ADMIN_PASSWORD", "")
APP_SECRET = os.getenv("APP_SECRET", "")

for folder in (BOTS_DIR, LOGS_DIR):
    folder.mkdir(parents=True, exist_ok=True)

if not ADMIN_USER or not ADMIN_PASS or not APP_SECRET:
    raise RuntimeError(
        "Set ADMIN_USERNAME, ADMIN_PASSWORD and APP_SECRET "
        "in Render Environment Variables."
    )

app = FastAPI(title=APP_NAME)

app.add_middleware(
    SessionMiddleware,
    secret_key=APP_SECRET,
    https_only=True,
    same_site="lax",
    max_age=3600,
)

bots = {}
processes = {}
lock = threading.Lock()


# ================= STORAGE =================

def load_bots():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_bots():
    CONFIG_FILE.write_text(json.dumps(bots, indent=2))


bots.update(load_bots())


def is_logged_in(request):
    return request.session.get("authenticated") is True


def safe_id(bot_id):
    return bot_id if bot_id in bots else None


def bot_status(bot_id):
    process = processes.get(bot_id)
    if process and process.poll() is None:
        return "RUNNING"
    return "STOPPED"


def read_log(bot_id):
    path = LOGS_DIR / f"{bot_id}.log"
    if not path.exists():
        return "No logs yet."
    try:
        return path.read_text(errors="replace")[-12000:]
    except OSError:
        return "Unable to read logs."


def page(content, request):
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=303)

    return HTMLResponse(f"""
    <!doctype html>
    <html>
    <head>
      <meta name="viewport" content="width=device-width,initial-scale=1">
      <title>{APP_NAME}</title>
      <style>
        body {{
          background:#10131c;color:#f3f4f6;
          font-family:Arial,sans-serif;
          max-width:850px;margin:25px auto;padding:16px
        }}
        .card {{
          background:#1d2332;padding:18px;margin:15px 0;
          border-radius:14px;overflow-wrap:anywhere
        }}
        input,button {{
          padding:12px;margin:6px 0;max-width:100%;
          border-radius:7px;border:0
        }}
        input[type=text],input[type=password] {{width:90%}}
        button {{background:#16a34a;color:white;cursor:pointer}}
        a {{color:#60a5fa}}
        pre {{white-space:pre-wrap;overflow-wrap:anywhere}}
      </style>
    </head>
    <body>
      <h1>🚀 {APP_NAME}</h1>
      <p>Personal Telegram Bot Manager</p>
      {content}
      <p><a href="/logout">Logout</a></p>
    </body>
    </html>
    """)


# ================= LOGIN =================

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=303)

    cards = ""

    for bot_id, info in bots.items():
        name = info.get("name", bot_id)
        status = bot_status(bot_id)

        cards += f"""
        <div class="card">
          <h3>{name}</h3>
          <p>ID: {bot_id}</p>
          <p>Status: <b>{status}</b></p>
          <a href="/logs/{bot_id}">📋 View Logs</a>
          <form method="post" action="/start/{bot_id}">
            <button>▶ Start</button>
          </form>
          <form method="post" action="/stop/{bot_id}">
            <button style="background:#dc2626">■ Stop</button>
          </form>
          <form method="post" action="/delete/{bot_id}">
            <button style="background:#6b7280">Delete</button>
          </form>
        </div>
        """

    content = """
    <div class="card">
      <h2>📤 Upload Bot</h2>
      <form action="/upload" method="post" enctype="multipart/form-data">
        <input type="file" name="file" accept=".py,.zip" required>
        <br>
        <button type="submit">Upload Bot</button>
      </form>
      <p>ZIP mein Python entry file aur optional requirements.txt rakho.</p>
    </div>
    <div class="card">
      <h2>🔑 Bot Token</h2>
      <p>Token ko public source code mein mat rakho.</p>
      <p>Render Dashboard → Environment → BOT_TOKEN</p>
      <p>Alag bots ke liye alag environment variables use karo.</p>
    </div>
    """ + cards

    return page(content, request)


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return HTMLResponse("""
    <!doctype html>
    <html><head>
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>ADII X HOST Login</title>
    </head>
    <body style="font-family:Arial;max-width:400px;margin:60px auto;padding:20px">
      <h2>🔐 ADII X HOST</h2>
      <form method="post" action="/login">
        <input name="username" placeholder="Username" required>
        <br><br>
        <input name="password" type="password"
               placeholder="Password" required>
        <br><br>
        <button type="submit">Login</button>
      </form>
    </body></html>
    """)


@app.post("/login")
def login(request: Request,
          username: str = Form(...),
          password: str = Form(...)):
    if username == ADMIN_USER and password == ADMIN_PASS:
        request.session.clear()
        request.session["authenticated"] = True
        return RedirectResponse("/", status_code=303)

    return HTMLResponse("Invalid credentials", status_code=401)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ================= UPLOAD =================

@app.post("/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=303)

    filename = Path(file.filename or "").name

    if not filename.lower().endswith((".py", ".zip")):
        return HTMLResponse("Only .py and .zip files are supported.", 400)

    bot_id = uuid.uuid4().hex[:12]
    bot_dir = BOTS_DIR / bot_id
    bot_dir.mkdir(parents=True)

    upload_path = bot_dir / filename
    size = 0

    try:
        with upload_path.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break

                size += len(chunk)

                if size > 20 * 1024 * 1024:
                    raise ValueError("Upload limit is 20 MB.")

                out.write(chunk)

        if filename.lower().endswith(".zip"):
            extract_dir = bot_dir / "source"
            extract_dir.mkdir()

            # Prevent ZIP path traversal and symlink extraction.
            with zipfile.ZipFile(upload_path) as archive:
                members = archive.infolist()

                if len(members) > 500:
                    raise ValueError("Too many files in ZIP.")

                total_uncompressed = 0

                for member in members:
                    target = (extract_dir / member.filename).resolve()

                    if not target.is_relative_to(extract_dir.resolve()):
                        raise ValueError("Unsafe ZIP path.")

                    if (member.external_attr >> 16) & 0o170000 == 0o120000:
                        raise ValueError("ZIP symlinks are not allowed.")

                    total_uncompressed += member.file_size

                    if total_uncompressed > 100 * 1024 * 1024:
                        raise ValueError("Extracted ZIP is too large.")

                archive.extractall(extract_dir)

            upload_path.unlink()
            candidates = list(extract_dir.rglob("*.py"))
            candidates = [
                p for p in candidates
                if "__pycache__" not in p.parts
                and p.name != "setup.py"
            ]

            if not candidates:
                raise ValueError("ZIP does not contain a Python file
