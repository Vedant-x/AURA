from fastapi import FastAPI
from pydantic import BaseModel
from anthropic import Anthropic
from supabase import create_client
from dotenv import load_dotenv
import os

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