import os
import re
import json
import time
import uuid
import asyncio
import hashlib
from contextlib import asynccontextmanager
from typing import Optional, Union, Any, Tuple
from datetime import datetime
from zoneinfo import ZoneInfo
from tools.attachment_context import set_current_attachments

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, field_validator
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.auth.exceptions import RefreshError

import agent
from agent import (
    TOOL_STATUS_MESSAGES,
    set_compiled_graph,
    set_execution_context,
    workflow,
)
from tools.gmail_tools import execute_send_email_direct

GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata")

# The system prompt is only injected when a conversation thread is brand new.
# Bumping this value starts fresh threads (also clears any thread poisoned by a
# failed media message). Old history stays in SQLite but is no longer used.
THREAD_VERSION = os.getenv("THREAD_VERSION", "v6")


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def parse_and_decrypt_tokens(raw: Any) -> dict:
    """Parses token dicts, JSON strings, or decrypts AES-256-GCM ciphertexts if needed."""
    if isinstance(raw, dict):
        return raw

    if isinstance(raw, str):
        cleaned = raw.strip()
        if cleaned.startswith("{") and cleaned.endswith("}"):
            try:
                return json.loads(cleaned)
            except Exception:
                pass

        if ":" in cleaned:
            parts = cleaned.split(":")
            if len(parts) == 3:
                try:
                    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
                    raw_key = os.getenv("ENCRYPTION_KEY") or os.getenv("TOKEN_SECRET") or os.getenv("JWT_SECRET") or ""
                    if raw_key:
                        key = bytes.fromhex(raw_key) if len(raw_key) == 64 else hashlib.sha256(raw_key.encode()).digest()
                        iv = bytes.fromhex(parts[0])
                        tag = bytes.fromhex(parts[1])
                        encrypted = bytes.fromhex(parts[2])
                        aesgcm = AESGCM(key)
                        decrypted = aesgcm.decrypt(iv, encrypted + tag, None)
                        return json.loads(decrypted.decode("utf-8"))
                except Exception as err:
                    print(f"[AUTH PYTHON DECRYPT ERROR]: {err}")

    return {}


def token_is_fresh(tokens: dict, margin_s: int = 120) -> bool:
    """True if the access token exists and doesn't expire within `margin_s` seconds.
    Node's googleapis tokens carry expiry_date in milliseconds."""
    if not tokens.get("access_token"):
        return False
    try:
        return float(tokens.get("expiry_date")) / 1000.0 - time.time() > margin_s
    except (TypeError, ValueError):
        return False


def build_google_tokens(raw: Any) -> dict:
    tokens = parse_and_decrypt_tokens(raw)
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        return tokens

    client_id = tokens.get("client_id") or os.getenv("GOOGLE_CLIENT_ID")
    client_secret = tokens.get("client_secret") or os.getenv("GOOGLE_CLIENT_SECRET")
    token_uri = tokens.get("token_uri") or GOOGLE_TOKEN_URI

    if not client_id or not client_secret:
        return tokens

    creds = Credentials(
        token=tokens.get("access_token"),
        refresh_token=refresh_token,
        token_uri=token_uri,
        client_id=client_id,
        client_secret=client_secret,
    )

    # Only hit Google when the token is missing/expired (saves ~300ms per request)
    if not token_is_fresh(tokens):
        try:
            creds.refresh(GoogleAuthRequest())
        except RefreshError as e:
            print(f"[AUTH WARNING] Refresh token rejected: {e}")
            return tokens
        except Exception as e:
            print(f"[AUTH REFRESH ERROR] {e}")

    tokens.update({
        "token": creds.token,
        "access_token": creds.token,
        "refresh_token": refresh_token,
        "token_uri": token_uri,
        "client_id": client_id,
        "client_secret": client_secret,
    })
    return tokens


async def resolve_google_tokens(raw: Any) -> dict:
    try:
        return await asyncio.to_thread(build_google_tokens, raw)
    except Exception as e:
        print(f"[AUTH ERROR] {e}")
        return parse_and_decrypt_tokens(raw)


def extract_approval(tool_output) -> Optional[dict]:
    content = getattr(tool_output, "content", tool_output)
    if isinstance(content, list):
        content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    if not isinstance(content, str):
        return None
    marker = "[APPROVAL_REQUIRED:"
    start = content.find(marker)
    end = content.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        action = json.loads(content[start + len(marker):end])
    except json.JSONDecodeError:
        return None
    return {"action": action, "text": content[:start].strip()}


def extract_reminder(tool_output) -> Optional[dict]:
    content = getattr(tool_output, "content", tool_output)
    if isinstance(content, list):
        content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    if not isinstance(content, str):
        return None
    idx = content.find("[REMINDER:")
    if idx == -1:
        return None
    brace = content.find("{", idx)
    if brace == -1:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(content[brace:])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) and obj.get("message") else None

# ---------------------------------------------------------------------------
# Poll extraction + echo cleanup
# ---------------------------------------------------------------------------

POLL_MARKER = "[POLL:"


def extract_poll(text: str) -> Tuple[Optional[dict], str]:
    """
    Finds a [POLL: {...}] tag in the model's reply.
    Returns (poll_dict_or_None, text_with_tag_removed).
    The tag is always stripped, even if the JSON is invalid, so raw tags never reach Telegram.
    """
    idx = text.find(POLL_MARKER)
    if idx == -1:
        return None, text

    brace = text.find("{", idx)
    if brace == -1:
        return None, text.replace(POLL_MARKER, "").strip()

    try:
        obj, consumed = json.JSONDecoder().raw_decode(text[brace:])
    except ValueError:
        return None, (text[:idx]).strip()

    close = text.find("]", brace + consumed)
    tag_end = close + 1 if close != -1 else brace + consumed
    cleaned = (text[:idx] + text[tag_end:]).strip()

    if not isinstance(obj, dict):
        return None, cleaned

    question = str(obj.get("question", "")).strip()[:300]
    raw_options = obj.get("options", [])
    if not isinstance(raw_options, list):
        return None, cleaned

    options = []
    for o in raw_options:
        o = str(o).strip()[:100]
        if o and o not in options:
            options.append(o)
    options = options[:10]

    if not question or len(options) < 2:
        return None, cleaned

    poll = {
        "question": question,
        "options": options,
        "multiple": bool(obj.get("multiple", False)),
    }

    correct_index = obj.get("correct_index")
    if isinstance(correct_index, int) and not isinstance(correct_index, bool) and 0 <= correct_index < len(options):
        poll["correct_index"] = correct_index
        poll["multiple"] = False
        explanation = str(obj.get("explanation", "")).strip()[:200]
        if explanation:
            poll["explanation"] = explanation

    return poll, cleaned


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", s.lower())).strip()


_LIST_MARKER = re.compile(r"^\s*(?:[•\-\*]|\(?[A-Za-z0-9]{1,2}[\)\.:])\s+")
_LETTER_OPTION = re.compile(r"^\s*\(?[A-Da-d][\)\.:]\s+\S")


def strip_poll_echo(text: str, poll: dict) -> str:
    """
    Removes lines that merely repeat the poll (the question or one of its options)
    so the user doesn't see the same thing as raw text AND as a poll.
    Only drops lines that MATCH the poll; ordinary bullets (briefings etc.) are kept.
    """
    q = _norm(poll["question"])
    opts = {_norm(o) for o in poll["options"]}
    is_quiz = "correct_index" in poll

    kept = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            kept.append("")
            continue

        core = _norm(_LIST_MARKER.sub("", s))
        if core:
            if core == q or (len(q) > 15 and q in core):
                continue
            if core in opts:
                continue
        # quiz-style "A) Udaipur" lines, even if the text differs slightly
        if is_quiz and _LETTER_OPTION.match(s):
            continue
        kept.append(line)

    cleaned = "\n".join(kept)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncSqliteSaver.from_conn_string("otis_memory.sqlite") as checkpointer:
        compiled_graph = workflow.compile(checkpointer=checkpointer)
        set_compiled_graph(compiled_graph)
        print("✅ AsyncSqliteSaver connected & Otis Graph compiled successfully.")
        yield


app = FastAPI(title="Otis AI Brain Service", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    body = await request.body()
    print("\n--- [422 VALIDATION ERROR DETAILS] ---")
    print("Incoming body (truncated):", body.decode("utf-8", errors="ignore")[:500])
    print("Validation Errors:", exc.errors())
    print("--------------------------------------\n")
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


class MediaPayload(BaseModel):
    data: str
    mime_type: str
    filename: Optional[str] = None


class LocationPayload(BaseModel):
    latitude: float
    longitude: float


class AgentPayload(BaseModel):
    prompt: Optional[str] = ""
    chat_id: Union[str, int]
    google_tokens: Any = {}
    audio: Optional[MediaPayload] = None
    image: Optional[MediaPayload] = None
    location: Optional[LocationPayload] = None
    document: Optional[MediaPayload] = None  

    @field_validator("chat_id")
    @classmethod
    def coerce_chat_id(cls, v):
        return str(v)


class ConfirmActionPayload(BaseModel):
    chat_id: Union[str, int]
    google_tokens: Any = {}
    action_data: dict


@app.get("/health")
def health_check():
    return {"status": "ready", "service": "otis-brain"}


@app.post("/api/agent/confirm-action")
async def confirm_action(payload: ConfirmActionPayload):
    tokens = await resolve_google_tokens(payload.google_tokens)
    action = payload.action_data.get("action")

    if action == "send_email":
        result = await asyncio.to_thread(
            execute_send_email_direct,
            tokens=tokens,
            recipient=payload.action_data.get("recipient", ""),
            subject=payload.action_data.get("subject", ""),
            body=payload.action_data.get("body", ""),
            image_base64=payload.action_data.get("image_base64"),
            attachment_refs=payload.action_data.get("attachment_refs")
        )
        return {"result": result}

    return JSONResponse(status_code=400, content={"error": f"Unknown action: {action}"})


STATE_LOOKUP_TIMEOUT = 30
STALL_TIMEOUT = 90
HARD_TIMEOUT = 300


def build_system_instruction() -> str:
    # Refresh on every request so the time stays current
    current_time_str = datetime.now(ZoneInfo(TIMEZONE)).strftime("%A, %d %B %Y, %I:%M %p")

    return f"""You are Otis, a direct, decisive, proactive executive assistant on Telegram. You don't just wait for instructions: you notice things, take ownership of decisions, and execute.
    Now: {current_time_str} ({TIMEZONE}, UTC+05:30). All times are IST.

    TOOLS
    Email attachments: send_email(drive_files, image_paths, attach_chat_files)
    Gmail: search_email_threads, fetch_unread_emails, send_email
    Calendar: list_upcoming_events, create_calendar_event, update_calendar_event, delete_calendar_event
    Contacts: search_contact | Tasks: list_tasks, create_task, complete_task, delete_task
    Drive/Sheets: search_drive_files, read_drive_file, read_sheet_data, append_row_to_sheet, create_spreadsheet, clear_sheet_range, delete_sheet_row
    Web/Media: web_search, browse_url, search_image, generate_image, get_youtube_transcript, extract_video_id
    Environment: get_current_weather, get_commute_and_distance
    Reminders: set_reminder

    SPEED RULES
    - Call independent tools in parallel in a single step (e.g. Calendar + Gmail + Tasks together). Never call them one by one.
    - Don't repeat a lookup if the needed ID or email is already in this conversation.
    - Fetch only what's needed: small limits (5-10 items), no full-thread or full-page reads unless asked.
    - Don't ask permission for read-only actions. Act, then report.

    ACCURACY RULES
    - Never guess IDs or email addresses. Look them up first:
    • Update/cancel event: list_upcoming_events, then update/delete.
    • Complete/delete task: list_tasks, then act.
    • Email by name: search_contact, then send_email.
    - Any mention of a file, doc, PDF, invoice, resume or sheet: call search_drive_files, then read_drive_file with the file_id to get the content. Never say "I can't find it" without searching Drive first. Never use browse_url on Drive links.
    - If multiple matches exist for a contact, event or task, ask the user to pick one (use a poll, see POLLS). Don't guess.

    Exception: when the user wants a Drive file attached to an email, skip reading and use send_email(drive_files=...).

    MEDIA RULES
    - If the user sends a voice note: understand it, treat it as their request and act on it. Start your reply with a short "🎙️ You said: ..." line (one sentence) so they can see you heard correctly.
    - If the user sends an image (receipt, screenshot, document, whiteboard, product, etc.): describe what matters in one line, then do the obvious useful action (e.g. receipt -> offer to log it in a Sheet; event poster -> offer to add it to the calendar; screenshot of an email/chat -> offer a drafted reply).

    DECISION & DELEGATION RULES
    - If the user asks YOU to decide or choose between options ("pick X or Y for me", "choose between A and B", "which one should I get/do/eat?", "u choose", "pick for me"):
      • NEVER create a poll. The user asked YOU to make the call.
      • Make a decisive pick immediately in 1-2 lines with a crisp rationale.
      • Examples:
        User: "pick biryani or roti for me"
        Otis: "Go with Biryani 🍲 — more filling and saves you from cooking sides." (NO POLL)

        User: "choose between Gym at 6 PM or study session"
        Otis: "Hit the gym at 6 PM 🏋️. Clear your head first, then study with higher focus." (NO POLL)

    POLLS
    You can send the user a native Telegram poll by putting this tag on its own line at the END of your reply:
    [POLL: {{"question": "...", "options": ["Option 1", "Option 2"], "multiple": false}}]
    The poll is shown natively, so NEVER repeat the poll question or its options as text anywhere in your reply. Your text should only be the short context before it.

    STRICT POLL RULES:
    1. NEVER create a poll when the user says "pick...", "choose for me", "which one", or asks your opinion.
    2. ONLY send a poll in these 2 scenarios:
       a) The user explicitly asks for a poll (e.g., "create a poll", "make a poll", "poll on...").
       b) You are executing a tool task and genuinely need the user's input to proceed because guessing would be costly (e.g., "Found 3 contacts named Alex — which one should I email?").
    3. Never poll for personal choices, casual questions, or food/leisure recommendations.

    BE PROACTIVE (normal chat)
    - Take initiative. While doing a task, if you notice a calendar conflict, an approaching deadline, an overdue task or an urgent email, mention it in one short line.
    - After finishing a request, if there is an obvious and valuable next step, offer it: either one short line, or ONE poll when there are 2-4 concrete options. Examples:
      • You listed emails that need a reply: poll "Which should I draft a reply for first?" (options = the senders, plus "None for now").
      • You listed overdue tasks: poll "What should I do with these?" (e.g. "Reschedule to tomorrow", "Mark done", "Leave as is").
      • You created or moved an event: mention conflicts or back-to-back meetings you noticed.
      • You summarized a file or page: offer the single most useful follow-up (e.g. "Want this emailed to someone?").
    - Max ONE follow-up per reply. Skip follow-ups after simple factual answers, small talk, or when the user clearly just wants the answer. Never nag or repeat a follow-up the user ignored.
    - For clear, low-risk requests just do them. Only ask when a wrong guess would be costly or the possibilities are genuinely unclear.

    QUIZZES
    When the user asks for a quiz, trivia, or practice questions:
    - Send exactly ONE question per turn.
    - STRICT FORMAT RULE: The text message must ONLY contain the header (e.g. "Question 1 of 5:").
      DO NOT print the question in the text message.
      DO NOT list the options (A, B, C, D) in the text message.
      Everything belongs exclusively inside the [POLL: ...] tag!

    - Quiz Tag Format:
      Question 1 of 5:
      [POLL: {{"question": "Which Indian city is known as the Pink City?", "options": ["Udaipur", "Jodhpur", "Jaipur", "Agra"], "correct_index": 2, "explanation": "Jaipur was painted pink in 1876 to welcome Prince Albert."}}]

    - NEVER do this:
      ❌ BAD:
      Here is your question:
      Which city is...?
      A) Udaipur
      B) Jaipur
      [POLL: ...]

    - Default to 5 questions unless specified.
    - correct_index is the 0-based integer index of the correct option.
    - Telegram requires explanations to be under 200 characters.
    - No LaTeX and no $ signs: write plain text like O(N log N) or N^2.
    
        REMINDERS & SCHEDULED TASKS
    - When the user asks to be reminded, to set an alarm or timer, or to do something on a schedule, call set_reminder. Never claim a reminder is set without calling it.
    - One-time: remind_at = ISO-8601 with +05:30, computed from the current time above ("in 20 minutes", "tomorrow 7 AM"). If the time is missing, ask. If today's time already passed, use the next occurrence.
    - Repeating: cron with 5 fields in IST. "every weekday 9 AM" = 0 9 * * 1-5. "every Sunday 6 PM" = 0 18 * * 0.
    - Plain reminder: message is short text addressed to the user ("Call Rahul about the invoice").
    - If the user wants you to DO something at that time (check mail, weather, tasks, summarize), set run_agent=true and write message as an instruction to yourself ("Fetch my unread emails and summarize the important ones").
    - "Remind me" or "alarm" means set_reminder, not a calendar event, unless the user says calendar.
    - Several reminders in one message: call set_reminder once per reminder, in parallel.
    - Confirm in ONE short line. The gateway appends the exact time, so don't repeat it.


    AUTOMATED RUNS
    Messages that start with [AUTOMATED:...] come from the scheduler, not from the user. Never greet, never ask "How can I assist you?", always fetch the data in parallel first.
    - [AUTOMATED:morning_briefing]: fetch Calendar (today), Gmail (unread) and Tasks (pending). Report sections: *📅 Schedule* • *📧 Priority Emails* • *✅ Tasks Due* • *⚠️ Needs Attention*. Then the line "Hope you have a good day! 😊". If there are 2-4 clear priorities, add ONE poll after that line asking which to start with.
    - [AUTOMATED:evening_wrapup]: fetch pending tasks, tomorrow's calendar and unread important emails. Short summary: what's pending, tomorrow's first events, anything urgent. If tasks are pending, add ONE poll about what to do with them.
    - [AUTOMATED:heartbeat]: a quick background check (events in the next 45 minutes, important/needs-reply unread emails, overdue or due-today tasks). Report only items that are NEW. Do not repeat things you already alerted the user about earlier in this conversation. If nothing new is worth interrupting for, reply with exactly [NO_ALERT] and nothing else (no other words, no emoji, no tool summary). Otherwise a SHORT alert, max 6 lines, with ONE poll only if the user must choose how to handle something.
    - If the heartbeat was triggered on demand ("On-demand check") and nothing needs attention, reply in one line: All clear ✅.
    - [AUTOMATED:scheduled_task]: a task the user scheduled earlier. Do the instruction after the tag now. No greeting, concise result.

    ACTION RULES
    - send_email: always include explicit recipient, subject and body. If the user wants edits, revise and re-invoke send_email.
    - Attachments in email: (a) a photo or file the user just sent in chat: send_email(attach_chat_files=true). (b) a Drive file: do NOT read it, just pass its name or id: send_email(drive_files="Invoice.pdf"); separate several with " | ". If it reports several matches, ask the user which one (poll), then retry with the id. (c) a generated image: call generate_image, take the path from [IMAGE_PATH: ...] and call send_email(image_paths="<path>"). (d) revising a draft: pass the attachment_refs from the earlier draft so the files are kept. Web images from search_image can't be attached: offer to send the link instead.
    - Calendar times: ISO-8601 with offset, YYYY-MM-DDTHH:MM:SS+05:30. Default duration is 30 minutes. Resolve "tomorrow", "Friday" etc. relative to the current date above.
    EMAIL RULES
    - Use clean, natural paragraph breaks. NEVER output literal "\\n", "\\\\n", "/n", or escaped slash characters in the text or tool arguments.
    - The email body must contain ONLY the message itself: greeting, concise paragraphs, and sign-off.
    If the user sends a photo or file and wants it emailed, call send_email with attach_chat_files=true. For generated images use image_paths. For Drive files use drive_files.
    - NEVER put subject lines, placeholders, markdown code fences, or conversational preambles (e.g. "Here is the draft:", "Sure, I wrote this:") inside the email body or inside `send_email` parameters.
    - When previewing an email in Telegram before sending, format it strictly as:
      *To:* recipient@example.com
      *Subject:* Clear Subject Line

      Hi [Name],

      [Body message]

      Best,
      [User Name]


    OUTPUT
    - After any tool call, always send a short text summary of the result (the only exception is a heartbeat with nothing new: reply exactly [NO_ALERT]).
    - Concise, executive tone. No filler.
    - search_image result: put [IMAGE_URL: <url>] on its own line.
    - generate_image result: keep [IMAGE_PATH: <path>] exactly as returned, on its own line. Never convert it to IMAGE_URL.
    - Format: Telegram Markdown with *bold* (single asterisks) and • bullets.

    """


# ---------------------------------------------------------------------------
# Building the user message (text / image / audio)
# ---------------------------------------------------------------------------

def build_user_message(payload: AgentPayload, msg_id: str) -> Tuple[HumanMessage, Optional[str], str]:
    """
    Returns (message, media_kind_or_None, plain_prompt_text).

    - Images use the OpenAI-style image_url data URI (supported for images).
    - Audio MUST use the "media" part type. Sending audio as image_url is what
      produced "contents are required".
    """
    loc_context = ""
    if payload.location:
        loc_context = (
            f"\n[User Shared Real-time Coordinates: "
            f"Latitude {payload.location.latitude}, Longitude {payload.location.longitude}]"
        )

    file_note = ""
    if payload.document and payload.document.data:
        kb = int(len(payload.document.data) * 3 / 4 / 1024)
        file_note = (
            f"\n[User attached a file: \"{payload.document.filename or 'document'}\" "
            f"({payload.document.mime_type}, ~{kb} KB). To email it, call send_email with attach_chat_files=true.]"
        )
    elif payload.image and payload.image.data:
        file_note = "\n[To email this photo, call send_email with attach_chat_files=true.]"

    prompt_content = ((payload.prompt or "") + loc_context + file_note).strip()

    if payload.image and payload.image.data:
        text = prompt_content or "Analyze this image and execute any relevant tools."
        mime = payload.image.mime_type or "image/jpeg"
        content = [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{payload.image.data}"}},
        ]
        return HumanMessage(content=content, id=msg_id), "image", text

    if payload.audio and payload.audio.data:
        text = prompt_content or "Listen to this audio note and execute any requests."
        mime = payload.audio.mime_type or "audio/ogg"
        content = [
            {"type": "text", "text": text},
            {"type": "media", "mime_type": mime, "data": payload.audio.data},
        ]
        return HumanMessage(content=content, id=msg_id), "audio", text

    text = prompt_content or "Hello"
    return HumanMessage(content=text, id=msg_id), None, text


async def scrub_media_message(config: dict, msg_id: str, text: str, kind: str) -> None:
    """
    Replaces the media-bearing message in the checkpoint with a text-only
    placeholder (same id => the add_messages reducer overwrites it). This stops
    base64 blobs from bloating history and stops a bad media message from
    breaking every later turn.
    """
    try:
        await agent.otis_graph.aupdate_state(
            config,
            {"messages": [HumanMessage(content=f"{text}\n[the user's {kind} attachment was processed and removed from history]", id=msg_id)]},
        )
    except Exception as e:
        print(f"[SCRUB WARNING] Could not scrub {kind} message from history: {e}")


# ---------------------------------------------------------------------------
# Streaming endpoint
# ---------------------------------------------------------------------------

@app.post("/api/agent/dispatch-stream")
async def dispatch_prompt_stream(payload: AgentPayload):
    if agent.otis_graph is None:
        raise HTTPException(status_code=503, detail="Agent brain is still initializing.")

    tokens = await resolve_google_tokens(payload.google_tokens)
    set_execution_context(tokens, payload.chat_id)

    system_instruction = build_system_instruction()
    config = {"configurable": {"thread_id": f"{payload.chat_id}_{THREAD_VERSION}"}}

    msg_id = str(uuid.uuid4())
    new_user_message, media_kind, plain_text = build_user_message(payload, msg_id)
    chat_files = [
        {"data": m.data, "mime_type": m.mime_type, "filename": m.filename}
        for m in (payload.image, payload.document)
        if m and m.data
    ]
    try:
        state = await asyncio.wait_for(
            agent.otis_graph.aget_state(config), timeout=STATE_LOOKUP_TIMEOUT
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail="Timed out reading conversation state. Please try again.",
        )

    messages_to_send = (
        [SystemMessage(content=system_instruction), new_user_message]
        if not state or not state.values.get("messages")
        else [new_user_message]
    )

    async def sse_generator():
        queue: "asyncio.Queue" = asyncio.Queue()
        final_reply_holder = {"text": ""}
        approval_holder = {"data": None}
        reminder_holder = []

        async def run_graph(msgs):
            async for event in agent.otis_graph.astream_events(
                {"messages": msgs},
                config=config,
                version="v2",
            ):
                await queue.put(("event", event))

        async def producer():
            try:
                try:
                    await run_graph(messages_to_send)
                except Exception as first_err:
                    # Media turn failed before producing a reply: clean the poisoned
                    # message out of history and retry once as text-only.
                    if not media_kind or final_reply_holder["text"]:
                        raise
                    print(f"[MEDIA RETRY] {media_kind} turn failed ({first_err}); retrying text-only.")
                    await scrub_media_message(config, msg_id, plain_text, media_kind)
                    fallback = HumanMessage(
                        content=(
                            f"{plain_text}\n[System note: the user's {media_kind} attachment could not be "
                            f"processed. Tell them briefly and ask them to resend it or describe it in text.]"
                        )
                    )
                    await run_graph([fallback])
            except Exception as e:
                await queue.put(("error", str(e)))
            finally:
                await queue.put(("done", None))

        set_current_attachments(chat_files)
        producer_task = asyncio.create_task(producer())
        start = asyncio.get_event_loop().time()

        try:
            while True:
                elapsed = asyncio.get_event_loop().time() - start
                if elapsed > HARD_TIMEOUT:
                    producer_task.cancel()
                    timeout_msg = "⚠️ This request took longer than expected and was stopped."
                    yield f"data: {json.dumps({'type': 'final', 'response': timeout_msg})}\n\n"
                    return

                try:
                    kind_wrapper, item = await asyncio.wait_for(
                        queue.get(), timeout=STALL_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type': 'status', 'message': '⏳ Still working on it...'})}\n\n"
                    continue

                if kind_wrapper == "done":
                    break

                if kind_wrapper == "error":
                    print(f"Error during stream: {item}")
                    if media_kind:
                        await scrub_media_message(config, msg_id, plain_text, media_kind)
                    err_msg = "⚠️ Otis encountered an error while processing your request."
                    yield f"data: {json.dumps({'type': 'final', 'response': err_msg})}\n\n"
                    return

                event = item
                kind = event.get("event")

                if kind == "on_tool_start":
                    tool_name = event.get("name", "")
                    status = TOOL_STATUS_MESSAGES.get(tool_name, f"⚙ Running {tool_name}...")
                    yield f"data: {json.dumps({'type': 'status', 'message': status})}\n\n"

                elif kind == "on_tool_end" and event.get("name") == "send_email":
                    approval = extract_approval(event.get("data", {}).get("output"))
                    if approval:
                        approval_holder["data"] = approval
                
                elif kind == "on_tool_end" and event.get("name") == "set_reminder":
                    spec = extract_reminder(event.get("data", {}).get("output"))
                    if spec:
                        reminder_holder.append(spec)        

                elif kind == "on_chat_model_end":
                    output = event.get("data", {}).get("output")
                    if output and hasattr(output, "content"):
                        content = output.content
                        if isinstance(content, str) and content.strip():
                            final_reply_holder["text"] = content.strip()
                        elif isinstance(content, list):
                            texts = []
                            for b in content:
                                if isinstance(b, str) and b.strip():
                                    texts.append(b.strip())
                                elif isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                                    texts.append(b.get("text", "").strip())
                            if texts:
                                final_reply_holder["text"] = "\n".join(texts)

            # Strip base64 media from history now that the turn is done
            if media_kind:
                await scrub_media_message(config, msg_id, plain_text, media_kind)

            if not final_reply_holder["text"].strip():
                try:
                    latest_state = await agent.otis_graph.aget_state(config)
                    if latest_state and latest_state.values.get("messages"):
                        for msg in reversed(latest_state.values["messages"]):
                            content = getattr(msg, "content", None)
                            if isinstance(content, str) and content.strip():
                                final_reply_holder["text"] = content.strip()
                                break
                            elif isinstance(content, list):
                                extracted_parts = []
                                for part in content:
                                    if isinstance(part, str) and part.strip():
                                        extracted_parts.append(part.strip())
                                    elif isinstance(part, dict) and part.get("type") == "text":
                                        txt = part.get("text", "").strip()
                                        if txt:
                                            extracted_parts.append(txt)
                                if extracted_parts:
                                    final_reply_holder["text"] = "\n".join(extracted_parts)
                                    break
                except Exception as recovery_err:
                    print(f"Failed to recover final message: {recovery_err}")

            final_text = final_reply_holder["text"].strip()

            # Pull out a [POLL: {...}] tag and remove any text that just echoes it
            poll, final_text = extract_poll(final_text)
            if poll:
                final_text = strip_poll_echo(final_text, poll)
                if not final_text:
                    final_text = "📊 Quick question:"

            # Heartbeat silence must be EXACTLY the token (no stray words)
            if not poll and "[NO_ALERT]" in final_text:
                final_text = "[NO_ALERT]"

            final_text = final_text or "✅ Done."

            if approval_holder["data"]:
                yield f"data: {json.dumps({'type': 'approval', **approval_holder['data']})}\n\n"

            if poll:
                yield f"data: {json.dumps({'type': 'poll', 'poll': poll})}\n\n"

            for spec in reminder_holder:
                yield f"data: {json.dumps({'type': 'reminder', 'reminder': spec})}\n\n"

            yield f"data: {json.dumps({'type': 'final', 'response': final_text})}\n\n"

        finally:
            if not producer_task.done():
                producer_task.cancel()

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    import uvicorn
    # reload=True is for development only; set RELOAD=false in production
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=os.getenv("RELOAD", "true") != "false")