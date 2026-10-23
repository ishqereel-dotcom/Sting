import os
import sys
import json
import uuid
import shutil
import zipfile
import subprocess
import threading
import html
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware


# ==================================================
# CONFIGURATION
# ==================================================

APP_NAME = "ADII X HOST"

BASE_DIR = Path(os.getenv("DATA_DIR", "/tmp/adii-host"))
BOTS_DIR = BASE_DIR / "bots"
LOGS_DIR = BASE_DIR / "logs"
CONFIG_FILE = BASE_DIR / "bots.json"

ADMIN_USER = os.getenv("ADMIN_USERNAME", "")
ADMIN_PASS = os.getenv("ADMIN_PASSWORD", "")
APP_SECRET = os.getenv("APP_SECRET", "")

MAX_UPLOAD_SIZE = 20 * 1024 * 1024
MAX_EXTRACTED_SIZE = 100 * 1024 * 1024

for directory in (BASE_DIR, BOTS_DIR, LOGS_DIR):
    directory.mkdir(parents=True, exist_ok=True)

if not ADMIN_USER or not ADMIN_PASS or not APP_SECRET:
    raise RuntimeError(
        "Set ADMIN_USERNAME, ADMIN_PASSWORD and APP_SECRET "
        "in Render Environment Variables."
    )


# ==================================================
# APPLICATION
# ==================================================

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
lock = threading.RLock()


# ==================================================
# DATABASE / STORAGE
# ==================================================

def load_bots():
    if not CONFIG_FILE.exists():
        return {}

    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_bots():
    temporary_file = CONFIG_FILE.with_suffix(".tmp")
    temporary_file.write_text(
        json.dumps(bots, indent=2),
        encoding="utf-8",
    )
    temporary_file.replace(CONFIG_FILE)


bots.update(load_bots())


def is_logged_in(request: Request):
    return request.session.get("authenticated") is True


def bot_exists(bot_id: str):
    return bot_id in bots


def get_status(bot_id: str):
    process = processes.get(bot_id)

    if process and process.poll() is None:
        return "🟢 RUNNING"

    return "🔴 STOPPED"


def read_log(bot_id: str):
    log_file = LOGS_DIR / f"{bot_id}.log"

    if not log_file.exists():
        return "No logs available."

    try:
        with log_file.open("rb") as file:
            file.seek(0, os.SEEK_END)
            size = file.tell()
            file.seek(max(0, size - 12000))
            data = file.read()

        return data.decode("utf-8", errors="replace")

    except OSError:
        return "Unable to read logs."


# ==================================================
# HTML LAYOUT
# ==================================================

def render_page(request: Request, content: str):
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=303)

    return HTMLResponse(
        """
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport"
                  content="width=device-width, initial-scale=1">
            <title>ADII X HOST</title>

            <style>
                * {
                    box-sizing: border-box;
                }

                body {
                    background: #10131c;
                    color: #f3f4f6;
                    font-family: Arial, sans-serif;
                    max-width: 850px;
                    margin: 20px auto;
                    padding: 16px;
                }

                .card {
                    background: #1d2332;
                    padding: 18px;
                    margin: 16px 0;
                    border-radius: 14px;
                    overflow-wrap: anywhere;
                }

                input, button {
                    padding: 12px;
                    margin: 7px 0;
                    max-width: 100%;
                    border-radius: 7px;
                }

                input {
                    color: #111827;
                }

                input[type="text"],
                input[type="password"] {
                    width: 100%;
                }

                button {
                    background: #16a34a;
                    color: white;
                    border: 0;
                    cursor: pointer;
                    font-weight: bold;
                }

                button:hover {
                    opacity: 0.85;
                }

                a {
                    color: #60a5fa;
                }

                pre {
                    background: #090b10;
                    color: #d1fae5;
                    padding: 12px;
                    border-radius: 8px;
                    white-space: pre-wrap;
                    overflow-wrap: anywhere;
                    overflow-x: auto;
                }

                .danger {
                    background: #dc2626;
                }

                .secondary {
                    background: #4b5563;
                }

                .small {
                    color: #aeb8cc;
                    font-size: 14px;
                }
            </style>
        </head>

        <body>
            <h1>🚀 ADII X HOST</h1>
            <p>Personal Telegram Bot Hosting Panel</p>
        """
        + content
        + """
            <hr>
            <p><a href="/logout">🚪 Logout</a></p>
        </body>
        </html>
        """
    )


# ==================================================
# LOGIN
# ==================================================

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if is_logged_in(request):
        return RedirectResponse("/", status_code=303)

    return HTMLResponse(
        """
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <meta name="viewport"
                  content="width=device-width, initial-scale=1">
            <title>ADII X HOST Login</title>
            <style>
                body {
                    background: #10131c;
                    color: white;
                    font-family: Arial, sans-serif;
                    max-width: 400px;
                    margin: 60px auto;
                    padding: 20px;
                }

                input, button {
                    width: 100%;
                    padding: 13px;
                    margin: 10px 0;
                    box-sizing: border-box;
                    border-radius: 8px;
                }

                button {
                    background: #16a34a;
                    color: white;
                    border: none;
                    font-weight: bold;
                }
            </style>
        </head>
        <body>
            <h2>🔐 ADII X HOST</h2>

            <form method="post" action="/login">
                <input name="username"
                       placeholder="Admin Username"
                       autocomplete="username"
                       required>

                <input name="password"
                       type="password"
                       placeholder="Admin Password"
                       autocomplete="current-password"
                       required>

                <button type="submit">Login</button>
            </form>
        </body>
        </html>
        """
    )


@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if username == ADMIN_USER and password == ADMIN_PASS:
        request.session.clear()
        request.session["authenticated"] = True

        return RedirectResponse("/", status_code=303)

    return HTMLResponse(
        "Invalid username or password.",
        status_code=401,
    )


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
