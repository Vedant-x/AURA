from fastapi import FastAPI
from pydantic import BaseModel
from anthropic import Anthropic
from supabase import create_client
from dotenv import load_dotenv
import os
import time

load_dotenv()

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

app = FastAPI()

class Message(BaseModel):
    text: str

@app.post("/chat")
def chat(msg: Message):
    result = supabase.table("memories").select("*").order("created_at", desc=True).limit(10).execute()
    past = list(reversed(result.data))

    messages = []
    for entry in past:
        user_msg = (entry.get("user_message") or "").strip()
        aura_msg = (entry.get("aura_reply") or "").strip()
        if user_msg and aura_msg:
            messages.append({"role": "user", "content": user_msg})
            messages.append({"role": "assistant", "content": aura_msg})

    messages.append({"role": "user", "content": msg.text})

    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=1024,
        messages=messages
    )

    reply_text = next(block.text for block in response.content if block.type == "text")

    supabase.table("memories").insert({
        "user_message": msg.text,
        "aura_reply": reply_text
    }).execute()

    return {"reply": reply_text}


screen_state = {
    "pending_question": None,
    "result": None,
}

class ScreenRequest(BaseModel):
    question: str

@app.post("/screen-request")
def screen_request(req: ScreenRequest):
    screen_state["pending_question"] = req.question
    screen_state["result"] = None
    return {"status": "queued"}

@app.get("/screen-pending")
def screen_pending():
    if screen_state["pending_question"]:
        return {"question": screen_state["pending_question"]}
    return {"question": None}

class ScreenUpload(BaseModel):
    image_base64: str
    question: str

@app.post("/screen-upload")
def screen_upload(payload: ScreenUpload):
    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=1024,
        messages=[
            {
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
            }
        ],
    )
    reply_text = next(block.text for block in response.content if block.type == "text")

    screen_state["pending_question"] = None
    screen_state["result"] = reply_text

    supabase.table("screen_logs").insert({
        "question": payload.question,
        "reply_summary": reply_text[:500]
    }).execute()

    return {"status": "done"}

@app.get("/screen-result")
def screen_result():
    if screen_state["result"]:
        result = screen_state["result"]
        screen_state["result"] = None
        return {"reply": result}
    return {"reply": None}


class AskRequest(BaseModel):
    text: str

@app.post("/ask")
def ask(req: AskRequest):
    classify = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=10,
        messages=[{
            "role": "user",
            "content": f"Does answering this question require seeing the user's computer screen right now? Reply with only YES or NO, nothing else.\n\nQuestion: {req.text}"
        }]
    )
    decision = next(block.text for block in classify.content if block.type == "text").strip().upper()

    if "YES" in decision:
        screen_state["pending_question"] = req.text
        screen_state["result"] = None

        waited = 0
        while waited < 25:
            time.sleep(1)
            waited += 1
            if screen_state["result"]:
                result = screen_state["result"]
                screen_state["result"] = None
                return {"reply": result}

        return {"reply": "I tried to check your screen but didn't get a response in time. Make sure the watcher is running."}

    else:
        return chat(Message(text=req.text))
