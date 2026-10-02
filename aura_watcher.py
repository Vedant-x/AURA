"""Poll AURA for screen questions from one trusted desktop session."""

import base64
import io
import logging
import os
from pathlib import Path
import sys
import time

import pyautogui
import requests

BASE_URL = os.getenv("AURA_URL", "https://aura-production-0486.up.railway.app").rstrip("/")
API_TOKEN = os.getenv("AURA_API_TOKEN")
POLL_SECONDS = 3
MAX_BACKOFF_SECONDS = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("aura-watcher")


def hold_single_instance_lock():
    """Keep the lock file open for the life of this process."""
    path = Path(__file__).with_name(".watcher.lock")
    lock_file = path.open("a+b")
    lock_file.seek(0)
    lock_file.write(b"0")
    lock_file.flush()
    lock_file.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        raise SystemExit("Another AURA watcher is already running")
    return lock_file


def check_and_respond(session):
    response = session.get(f"{BASE_URL}/screen-pending", timeout=10)
    response.raise_for_status()
    question = response.json().get("question")
    if not question:
        return

    log.info("Claimed a screen question")
    screenshot = pyautogui.screenshot()
    screenshot.thumbnail((1600, 1600))
    buffer = io.BytesIO()
    screenshot.save(buffer, format="PNG", optimize=True)
    image_bytes = buffer.getvalue()
    log.info("Captured PNG (%d bytes); uploading", len(image_bytes))

    response = session.post(
        f"{BASE_URL}/screen-upload",
        json={
            "image_base64": base64.b64encode(image_bytes).decode("ascii"),
            "question": question,
        },
        timeout=45,
    )
    response.raise_for_status()
    if response.json().get("status") != "done":
        raise RuntimeError("Backend did not confirm screen upload")
    log.info("Screen answer uploaded successfully")


def main():
    lock_file = hold_single_instance_lock()
    log.info("AURA watcher started: %s", BASE_URL)
    consecutive_failures = 0
    with requests.Session() as session:
        if API_TOKEN:
            session.headers["Authorization"] = f"Bearer {API_TOKEN}"
        while True:
            try:
                check_and_respond(session)
                consecutive_failures = 0
                time.sleep(POLL_SECONDS)
            except KeyboardInterrupt:
                break
            except Exception:
                consecutive_failures += 1
                delay = min(POLL_SECONDS * consecutive_failures, MAX_BACKOFF_SECONDS)
                log.exception("Watcher check failed; retrying in %ds", delay)
                time.sleep(delay)
    lock_file.close()


if __name__ == "__main__":
    main()
