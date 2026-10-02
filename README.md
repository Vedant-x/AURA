# AURA

AURA is a FastAPI backend for a personal assistant. A phone shortcut sends
`POST /ask`; Claude Haiku decides whether the question needs the current
computer screen. Ordinary questions go to Claude Sonnet with the last 10
exchanges from Supabase. Screen questions wait for a desktop watcher to take a
screenshot and upload it. Supabase stores chat exchanges and short screen logs,
not images.

## Components

- `main.py`: API and screen request coordination
- `aura_watcher.py`: desktop watcher for macOS or Windows
- `start_watcher.bat`: Windows Task Scheduler launcher with live logging
- `schema.sql`: database setup for a new Supabase project

## Setup

1. Install Python 3.10 or newer. Install `requirements.txt` for the API and
   `watcher-requirements.txt` on each desktop watcher.
2. For a new Supabase project, run `schema.sql` in the SQL editor. If the
   project does not expose new tables to its Data API by default, grant access
   to the backend's server-side key. Keep that key off client devices.
3. Copy `.env.example` to `.env` and fill in the backend variables.
4. Run `uvicorn main:app --host 127.0.0.1 --port 8000` locally, or deploy
   using the Procfile.
5. On each desktop, set `AURA_URL` to the deployed backend URL if it differs
   from the default in `aura_watcher.py`. Run the watcher from a logged-in
   desktop session with screenshot permission.

For a private deployment, set `AURA_API_TOKEN` to a long random value in the
backend and every watcher, and add `Authorization: Bearer <token>` to the phone
shortcut. The backend enforces the token when it is set. Update the phone and
all watchers **before** enabling it on the backend.

Before testing the phone, open `https://YOUR-DOMAIN/openapi.json`. It should
return API schema JSON. Railway's `Application not found` response means the
domain is not attached to a running service; watcher changes cannot fix that.

## Screen flow

The phone sends `POST /ask` with `{"text":"..."}`. For a screen question,
the watcher polls `GET /screen-pending`. That request atomically claims the
question, and the watcher posts `{"image_base64":"...","question":"..."}` to
`POST /screen-upload`. The API returns the answer to the waiting `/ask`.
The older `/screen-request` and `/screen-result` endpoints remain available.
Only one screen request can be active at a time, and abandoned requests expire.

Run one API worker: screen coordination is kept in process memory. A restart
loses pending requests. A persistent queue would be needed for multiple API
workers or reliable recovery across restarts.

## Windows Task Scheduler

Set the action to `start_watcher.bat` in the AURA folder and **Start in** to
that folder. Select **Run only when user is logged on**, because screenshots
need the interactive desktop. Set **If the task is already running** to
**Do not start a new instance**. The batch file moves to its own folder,
uses unbuffered Python, and writes `watcher_log.txt` there. The watcher also
uses a process lock to prevent duplicate copies.

## macOS launchd

Run the watcher with `python3 -u aura_watcher.py` from a logged-in user
session. A LaunchAgent should use the full interpreter and script paths,
`RunAtLoad`, `KeepAlive`, and explicit stdout/stderr log paths. Check its
actual state with `launchctl print gui/$(id -u)/com.aura.watcher`.

## Security limit

The API has one shared conversation history. Until `AURA_API_TOKEN` is set,
anyone who knows a reachable deployment URL could call it and incur AI costs
or read the next screen answer. The token protects one trusted personal setup;
multiple users would need separate authentication and memory ownership.
