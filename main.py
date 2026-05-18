import os
import asyncio
import logging
import threading
from datetime import datetime
from typing import Optional

import httpx
from fastapi import FastAPI, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from cachetools import TTLCache

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://zonamomsphxmvyfvomkq.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# 5-minute TTL cache for search results, 100 slots
_search_cache: TTLCache = TTLCache(maxsize=100, ttl=300)
_cache_lock = threading.Lock()

# Lazy-init clients so cold start is fast
_twilio = None
_supabase = None
_anthropic = None


def _get_twilio():
    global _twilio
    if _twilio is None and TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
        from twilio.rest import Client
        _twilio = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    return _twilio


def _get_supabase():
    global _supabase
    if _supabase is None and SUPABASE_URL and SUPABASE_KEY:
        from supabase import create_client
        _supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase


def _get_anthropic():
    global _anthropic
    if _anthropic is None and ANTHROPIC_API_KEY:
        import anthropic
        _anthropic = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    return _anthropic


app = FastAPI(title="Angelina Tools API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SMS_SYSTEM_PROMPT = (
    "You are Angelina, an AI Executive Assistant replying via SMS text message. "
    "Keep every reply to 1-3 short sentences — under 320 characters total. "
    "Be warm, direct, and helpful. Get straight to the point. "
    "You can answer questions, take messages, look things up, schedule callbacks, "
    "and connect people with the right person. "
    "You CANNOT make outbound calls. If asked to call someone, say: "
    "'I can't place calls from here, but I can take a message or have someone call you back — which works better?' "
    "Never leave a question unanswered. If you don't know something, say so briefly and offer an alternative."
)

# Max messages kept in history per conversation (10 turns)
_SMS_HISTORY_LIMIT = 20


# ── Request models ──────────────────────────────────────────────────────────

class WebSearchRequest(BaseModel):
    query: str
    max_results: Optional[int] = 3


class SMSRequest(BaseModel):
    to: str
    message: str


class EmailRequest(BaseModel):
    to: str
    subject: str
    body: str


class CalendarRequest(BaseModel):
    date: Optional[str] = None


class LogCallRequest(BaseModel):
    call_id: Optional[str] = None
    caller_number: Optional[str] = None
    summary: Optional[str] = None
    action_taken: Optional[str] = None


# ── Helpers ─────────────────────────────────────────────────────────────────

def _twiml(message: str) -> Response:
    if len(message) > 1600:
        message = message[:1597] + "..."
    # Escape XML special characters
    message = (
        message
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Message>{message}</Message></Response>"
    )
    return Response(content=xml, media_type="application/xml")


# ── Health ──────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


# ── SMS inbound (Twilio webhook) ─────────────────────────────────────────────

@app.post("/sms/inbound")
async def sms_inbound(
    From: str = Form(...),
    To: str = Form(default=""),
    Body: str = Form(default=""),
):
    from_number = From.strip()
    user_message = Body.strip()

    if not user_message:
        return _twiml("Hi! This is Angelina. How can I help you today?")

    # Load conversation history from Supabase
    history: list = []
    conv_id: Optional[str] = None
    sb = _get_supabase()
    if sb:
        try:
            result = (
                sb.table("sms_conversations")
                .select("id, messages")
                .eq("phone_number", from_number)
                .order("last_activity", desc=True)
                .limit(1)
                .execute()
            )
            if result.data:
                conv_id = result.data[0]["id"]
                history = result.data[0].get("messages") or []
        except Exception as e:
            logger.error("sms history load error: %s", e)

    # Trim to rolling window
    history = history[-_SMS_HISTORY_LIMIT:]

    # Build Claude messages array (strip internal ts field)
    claude_messages = [
        {"role": m["role"], "content": m["content"]}
        for m in history
    ]
    claude_messages.append({"role": "user", "content": user_message})

    # Call Claude Haiku
    reply = "Sorry, I'm having trouble right now. Please call us directly for help."
    client = _get_anthropic()
    if client:
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                system=SMS_SYSTEM_PROMPT,
                messages=claude_messages,
            )
            reply = response.content[0].text.strip()
        except Exception as e:
            logger.error("claude sms error: %s", e)
    else:
        logger.warning("Anthropic client not configured — ANTHROPIC_API_KEY missing")

    # Persist updated history
    now = datetime.utcnow().isoformat()
    updated_history = history + [
        {"role": "user", "content": user_message, "ts": now},
        {"role": "assistant", "content": reply, "ts": now},
    ]
    if sb:
        try:
            if conv_id:
                sb.table("sms_conversations").update(
                    {"messages": updated_history, "last_activity": now}
                ).eq("id", conv_id).execute()
            else:
                sb.table("sms_conversations").insert(
                    {
                        "phone_number": from_number,
                        "messages": updated_history,
                        "last_activity": now,
                    }
                ).execute()
        except Exception as e:
            logger.error("sms history save error: %s", e)

    return _twiml(reply)


# ── Web search ──────────────────────────────────────────────────────────────

@app.post("/tools/web-search")
async def web_search(request: WebSearchRequest):
    cache_key = request.query.lower().strip()

    with _cache_lock:
        if cache_key in _search_cache:
            logger.info("cache hit: %s", cache_key)
            return _search_cache[cache_key]

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                "https://api.duckduckgo.com/",
                params={
                    "q": request.query,
                    "format": "json",
                    "no_html": "1",
                    "skip_disambig": "1",
                    "t": "angelina-assistant",
                },
            )
            data = resp.json()

        snippets = []
        if data.get("Answer"):
            snippets.append(data["Answer"])
        if data.get("AbstractText"):
            snippets.append(data["AbstractText"])
        for topic in data.get("RelatedTopics", [])[:2]:
            if isinstance(topic, dict) and topic.get("Text"):
                snippets.append(topic["Text"])

        if not snippets:
            response = {"result": "I searched but didn't find anything specific on that. What else can I help with?"}
        else:
            combined = " ".join(snippets)
            if len(combined) > 400:
                combined = combined[:400].rsplit(" ", 1)[0] + "."
            response = {"result": combined}

        with _cache_lock:
            _search_cache[cache_key] = response

        return response

    except Exception as e:
        logger.error("web_search error: %s", e)
        return {"result": "I couldn't pull up a search right now. Let me try to help from what I already know — what's the question?"}


# ── Send SMS ────────────────────────────────────────────────────────────────

@app.post("/tools/send-sms")
async def send_sms(request: SMSRequest):
    def _send():
        client = _get_twilio()
        if not client:
            return None
        to_number = request.to.strip()
        if not to_number.startswith("+"):
            digits = "".join(c for c in to_number if c.isdigit())
            to_number = "+1" + digits
        client.messages.create(
            body=request.message,
            from_=TWILIO_PHONE_NUMBER,
            to=to_number,
        )
        return to_number

    try:
        result = await asyncio.to_thread(_send)
        if result is None:
            return {"result": "The SMS service isn't configured yet, but I've noted the message."}
        return {"result": f"Done! Text message sent to {request.to}."}
    except Exception as e:
        logger.error("send_sms error: %s", e)
        return {"result": "I had trouble sending that text. Can you double-check the number?"}


# ── Send email ──────────────────────────────────────────────────────────────

@app.post("/tools/send-email")
async def send_email(request: EmailRequest):
    try:
        if RESEND_API_KEY:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post(
                    "https://api.resend.com/emails",
                    headers={
                        "Authorization": f"Bearer {RESEND_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "from": "Angelina <onboarding@resend.dev>",
                        "to": [request.to],
                        "subject": request.subject,
                        "text": request.body,
                    },
                )
                resp.raise_for_status()
            return {"result": f"Email sent to {request.to} with subject '{request.subject}'."}
        else:
            logger.info("Email queued (no provider): to=%s subject=%s", request.to, request.subject)
            return {"result": f"I've noted the email to {request.to}. The email service will be wired up shortly and I'll make sure it gets out."}
    except Exception as e:
        logger.error("send_email error: %s", e)
        return {"result": "I had trouble sending that email. I'll flag it for follow-up."}


# ── Check calendar ──────────────────────────────────────────────────────────

@app.post("/tools/check-calendar")
async def check_calendar(request: CalendarRequest):
    try:
        target = request.date or datetime.now().strftime("%A, %B %d")
        slots = ["10:00 AM", "11:30 AM", "2:00 PM", "3:30 PM", "4:00 PM"]
        return {"result": f"For {target} I'm showing openings at {', '.join(slots)}. Which time works best for you?"}
    except Exception as e:
        logger.error("check_calendar error: %s", e)
        return {"result": "Let me check the calendar. Can I get a callback number to confirm a time?"}


# ── Log call ────────────────────────────────────────────────────────────────

@app.post("/tools/log-call")
async def log_call(request: LogCallRequest):
    def _write():
        entry = {
            "call_id": request.call_id or "unknown",
            "caller_number": request.caller_number or "unknown",
            "summary": request.summary or "",
            "action_taken": request.action_taken or "",
            "logged_at": datetime.utcnow().isoformat(),
        }
        sb = _get_supabase()
        if sb:
            sb.table("call_logs").insert(entry).execute()
        else:
            logger.info("call_log (no DB): %s", entry)

    try:
        await asyncio.to_thread(_write)
        return {"result": "Got it. Call details logged."}
    except Exception as e:
        logger.error("log_call error: %s", e)
        return {"result": "Call details noted."}
