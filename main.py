"""
AURA backend — chat, persistent memory, and screen awareness.
"""

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel
from anthropic import Anthropic
from supabase import create_client
from dotenv import load_dotenv
import os
import asyncio
import logging
import secrets
import threading
import time

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("aura")

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

def require_api_token(authorization: str = Header(default="")):
    """Enable bearer authentication when AURA_API_TOKEN is configured."""
    token = os.getenv("AURA_API_TOKEN")
    if token:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(value, token):
            raise HTTPException(status_code=401, detail="Invalid or missing API token")


app = FastAPI(title="AURA", dependencies=[Depends(require_api_token)])

# ---------------------------------------------------------------------------
# Config — tune behavior here instead of hunting through the file
# ---------------------------------------------------------------------------

CHAT_MODEL = "claude-sonnet-5"
CLASSIFY_MODEL = "claude-haiku-4-5-20251001"   # cheap + fast, plenty for a yes/no call
MEMORY_LOOKBACK = 10          # how many past exchanges to include as context
LOG_SUMMARY_CHARS = 500       # how much of a screen reply to store in screen_logs
SCREEN_WAIT_TIMEOUT = 25      # seconds to wait for a watcher to respond
SCREEN_POLL_INTERVAL = 1      # seconds between checks while waiting
SCREEN_REQUEST_TTL = 120      # clear abandoned direct screen requests

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_text(response, default="I couldn't generate a response that time — try again.") -> str:
    """Pull the first text block out of a Claude response, with a safe fallback."""
    return next((b.text for b in response.content if b.type == "text"), default)


def call_claude(**kwargs) -> str:
    """Return a real answer or surface the upstream failure to the client."""
    try:
        response = client.messages.create(**kwargs)
    except Exception:
        logger.exception("Claude API call failed")
        raise HTTPException(status_code=502, detail="AI service unavailable")
    reply = extract_text(response, default="")
    if not reply:
        raise HTTPException(status_code=502, detail="AI service returned no text")
    return reply


# ---------------------------------------------------------------------------
# Chat + memory
# ---------------------------------------------------------------------------

class Message(BaseModel):
    text: str

@app.post("/chat")
def chat(msg: Message):
    if not msg.text.strip():
        return {"reply": "Say something and I'll respond."}

    result = (
        supabase.table("memories")
        .select("*")
        .order("created_at", desc=True)
        .limit(MEMORY_LOOKBACK)
        .execute()
    )
    past = list(reversed(result.data))

    messages = []
    for entry in past:
        user_msg = (entry.get("user_message") or "").strip()
        aura_msg = (entry.get("aura_reply") or "").strip()
        if user_msg and aura_msg:
            messages.append({"role": "user", "content": user_msg})
            messages.append({"role": "assistant", "content": aura_msg})
    messages.append({"role": "user", "content": msg.text})

    reply_text = call_claude(model=CHAT_MODEL, max_tokens=1024, messages=messages)

    supabase.table("memories").insert({
        "user_message": msg.text,
        "aura_reply": reply_text,
    }).execute()

    return {"reply": reply_text}


# ---------------------------------------------------------------------------
# Screen awareness
#
# A watcher script on each machine (Mac / Windows) polls /screen-pending
# every few seconds. /screen-pending clears the question the instant it's
# read, so with multiple watchers running at once, only the first one to
# poll ever claims a given request — no duplicate screenshots, no wasted
# API calls, no race on which answer "wins".
# ---------------------------------------------------------------------------

screen_state = {
    "pending_question": None,
    "active_question": None,
    "started_at": None,
    "result": None,
}
screen_lock = threading.Lock()

def expire_screen_request():
    """Caller must hold screen_lock."""
    started_at = screen_state["started_at"]
    if started_at is not None and time.monotonic() - started_at > SCREEN_REQUEST_TTL:
        screen_state.update(
            pending_question=None, active_question=None, started_at=None, result=None
        )

class ScreenRequest(BaseModel):
    question: str

@app.post("/screen-request")
def screen_request(req: ScreenRequest):
    if not req.question.strip():
        raise HTTPException(status_code=422, detail="Question cannot be empty")
    with screen_lock:
        expire_screen_request()
        if screen_state["active_question"] is not None:
            raise HTTPException(status_code=409, detail="A screen request is already in progress")
        screen_state["pending_question"] = req.question
        screen_state["active_question"] = req.question
        screen_state["started_at"] = time.monotonic()
        screen_state["result"] = None
    logger.info(f"Screen request queued: {req.question!r}")
    return {"status": "queued"}


@app.get("/screen-pending")
def screen_pending():
    """Atomic claim — first watcher to read this gets the question; it's
    cleared immediately so a second watcher polling moments later doesn't
    also pick it up."""
    with screen_lock:
        expire_screen_request()
        question = screen_state["pending_question"]
        screen_state["pending_question"] = None
    if question:
        logger.info(f"Screen request claimed: {question!r}")
        return {"question": question}
    return {"question": None}


class ScreenUpload(BaseModel):
    image_base64: str
    question: str

@app.post("/screen-upload")
def screen_upload(payload: ScreenUpload):
    with screen_lock:
        expire_screen_request()
        if payload.question != screen_state["active_question"] or screen_state["pending_question"]:
            raise HTTPException(status_code=409, detail="Screen request is not active")
    reply_text = call_claude(
        model=CHAT_MODEL,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": payload.image_base64,
                    },
                },
                {"type": "text", "text": payload.question},
            ],
        }],
    )

    with screen_lock:
        if payload.question != screen_state["active_question"]:
            raise HTTPException(status_code=410, detail="Screen request expired")
        screen_state["result"] = reply_text

    try:
        supabase.table("screen_logs").insert({
            "question": payload.question,
            "reply_summary": reply_text[:LOG_SUMMARY_CHARS],
        }).execute()
    except Exception:
        logger.exception("Failed to save screen log")

    logger.info("Screen reply ready.")
    return {"status": "done"}


@app.get("/screen-result")
def screen_result():
    with screen_lock:
        expire_screen_request()
        if screen_state["result"]:
            result = screen_state["result"]
            screen_state["result"] = None
            screen_state["active_question"] = None
            screen_state["started_at"] = None
            return {"reply": result}
    return {"reply": None}


# ---------------------------------------------------------------------------
# /ask — single smart entry point. Classifies intent (screen vs normal chat)
# and routes accordingly, so the client only ever needs to call one endpoint.
# Fully async so a 25-second screen wait never blocks other requests.
# ---------------------------------------------------------------------------

class AskRequest(BaseModel):
    text: str

@app.post("/ask")
async def ask(req: AskRequest):
    if not req.text.strip():
        return {"reply": "Say something and I'll respond."}

    decision = await asyncio.to_thread(
        call_claude,
        model=CLASSIFY_MODEL,
        max_tokens=10,
        messages=[{
            "role": "user",
            "content": (
                "The user is asking their AI assistant a question. Does answering it "
                "require looking at what's currently on the user's computer screen "
                "(laptop/Mac/PC — not their phone)? "
                "YES examples: 'what's on my screen', 'what am I looking at', "
                "'what's on my laptop', 'describe my screen', 'what app is open'. "
                "NO examples: general questions, facts, math, advice — anything not "
                "about current screen content. "
                "Reply with only YES or NO.\n\n"
                f"Question: {req.text}"
            ),
        }],
    )
    decision = decision.strip().upper()

    if not decision.startswith("YES"):
        return await asyncio.to_thread(chat, Message(text=req.text))

    with screen_lock:
        expire_screen_request()
        if screen_state["active_question"] is not None:
            raise HTTPException(status_code=409, detail="A screen request is already in progress")
        screen_state["result"] = None
        screen_state["pending_question"] = req.text
        screen_state["active_question"] = req.text
        screen_state["started_at"] = time.monotonic()

    try:
        waited = 0
        while waited < SCREEN_WAIT_TIMEOUT:
            await asyncio.sleep(SCREEN_POLL_INTERVAL)
            waited += SCREEN_POLL_INTERVAL
            with screen_lock:
                if screen_state["result"]:
                    result = screen_state["result"]
                    screen_state["result"] = None
                    return {"reply": result}
    finally:
        with screen_lock:
            screen_state["pending_question"] = None
            screen_state["active_question"] = None
            screen_state["started_at"] = None
            screen_state["result"] = None

    return {
        "reply": (
            "I tried to check your screen but didn't hear back in time. "
            "Make sure a watcher (Mac or Windows) is running."
        )
    }
