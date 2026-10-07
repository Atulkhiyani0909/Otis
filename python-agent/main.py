import os
import json
import asyncio
import hashlib
import tracemalloc
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional, Union, Any
from datetime import datetime
from zoneinfo import ZoneInfo

tracemalloc.start()

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

def parse_and_decrypt_tokens(raw: Any) -> dict:
    """Parses token dicts, JSON strings, or decrypts AES-256-GCM ciphertexts if needed."""
    if isinstance(raw, dict):
        return raw

    if isinstance(raw, str):
        cleaned = raw.strip()
        # Direct JSON string
        if cleaned.startswith("{") and cleaned.endswith("}"):
            try:
                return json.loads(cleaned)
            except Exception:
                pass

        # Encrypted ciphertext format (iv:tag:data)
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

    # Set token=None to force credentials to refresh and obtain a fresh access token
    creds = Credentials(
        token=tokens.get("access_token"),
        refresh_token=refresh_token,
        token_uri=token_uri,
        client_id=client_id,
        client_secret=client_secret,
    )

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
    print("Incoming Raw Body:", body.decode("utf-8", errors="ignore"))
    print("Validation Errors:", exc.errors())
    print("--------------------------------------\n")
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

class MediaPayload(BaseModel):
    data: str
    mime_type: str

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
        )
        return {"result": result}

    return JSONResponse(status_code=400, content={"error": f"Unknown action: {action}"})


STATE_LOOKUP_TIMEOUT = 30  
STALL_TIMEOUT = 90          
HARD_TIMEOUT = 300          

@app.post("/api/agent/dispatch-stream")
async def dispatch_prompt_stream(payload: AgentPayload):
    if agent.otis_graph is None:
        raise HTTPException(status_code=503, detail="Agent brain is still initializing.")

    tokens = await resolve_google_tokens(payload.google_tokens)
    set_execution_context(tokens, payload.chat_id)

    


    # Refresh this on every request or scheduled run so the time stays current
    current_time_str = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%A, %d %B %Y, %I:%M %p")

    system_instruction = f"""You are Otis, a direct, autonomous executive assistant on Telegram.
    Now: {current_time_str} (Asia/Kolkata, UTC+05:30). All times are IST.

    TOOLS
    Gmail: search_email_threads, fetch_unread_emails, send_email
    Calendar: list_upcoming_events, create_calendar_event, update_calendar_event, delete_calendar_event
    Contacts: search_contact | Tasks: list_tasks, create_task, complete_task, delete_task
    Drive/Sheets: search_drive_files, read_sheet_data, append_row_to_sheet, create_spreadsheet, clear_sheet_range, delete_sheet_row
    Web/Media: web_search, browse_url, search_image, generate_image, get_youtube_transcript, extract_video_id
    Environment: get_current_weather, get_commute_and_distance

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
    - Any mention of a file, doc, PDF, invoice, resume or sheet: call search_drive_files before replying. Never say "I can't find it" without searching Drive first.
    - If multiple matches exist for a contact, event or task, ask the user to pick one. Don't guess.

    AUTOMATED BRIEFS
    For "Morning Executive Briefing" or system scans:
    1. No greeting at the start and no questions. Immediately fetch Calendar (today), Gmail (unread) and Tasks (pending) in parallel.
    2. Output the report with these sections: *📅 Schedule* • *📧 Priority Emails* • *✅ Tasks Due* • *⚠️ Needs Attention*
    3. End the report with exactly this line: "Hope you have a good day! 😊"
    4. Never ask "How can I assist you?" or any other question.

    ACTION RULES
    - send_email: always include explicit recipient, subject and body. If the user wants edits, revise and re-invoke send_email.
    - Image in email: call generate_image or search_image first, then pass the base64 string to send_email(image_base64=...).
    - Calendar times: ISO-8601 with offset, YYYY-MM-DDTHH:MM:SS+05:30. Default duration is 30 minutes. Resolve "tomorrow", "Friday" etc. relative to the current date above.
    
    - Any mention of a file, doc, PDF, invoice, resume or sheet: call search_drive_files, then read_drive_file with the file_id to get the content. Never use browse_url on Drive links. 

    OUTPUT
    - After any tool call, always send a short text summary of the result.
    - Concise, executive tone. No filler.
    - search_image result: put [IMAGE_URL: <url>] on its own line.
    - generate_image result: keep [IMAGE_PATH: <path>] exactly as returned, on its own line. Never convert it to IMAGE_URL.
    - Format: Telegram Markdown with *bold* (single asterisks) and • bullets.
    """
    config = {"configurable": {"thread_id": str(payload.chat_id)}}

    loc_context = ""
    if payload.location:
        loc_context = f"\n[User Shared Real-time Coordinates: Latitude {payload.location.latitude}, Longitude {payload.location.longitude}]"

    prompt_content = (payload.prompt or "") + loc_context

    if payload.image:
        user_content = [
            {"type": "text", "text": prompt_content or "Analyze this image and execute any relevant tools."},
            {
                "type": "image_url",
                "image_url": {"url": f"data:{payload.image.mime_type};base64,{payload.image.data}"},
            },
        ]
        new_user_message = HumanMessage(content=user_content)
    elif payload.audio:
        user_content = [
            {"type": "text", "text": prompt_content or "Listen to this audio note and execute any requests."},
            {
                "type": "image_url",
                "image_url": {"url": f"data:{payload.audio.mime_type};base64,{payload.audio.data}"},
            },
        ]
        new_user_message = HumanMessage(content=user_content)
    else:
        new_user_message = HumanMessage(content=prompt_content or "Hello")

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

        async def producer():
            try:
                async for event in agent.otis_graph.astream_events(
                    {"messages": messages_to_send},
                    config=config,
                    version="v2",
                ):
                    await queue.put(("event", event))
            except Exception as e:
                await queue.put(("error", str(e)))
            finally:
                await queue.put(("done", None))

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

            final_text = final_reply_holder["text"].strip() or "✅ Done."

            if approval_holder["data"]:
                yield f"data: {json.dumps({'type': 'approval', **approval_holder['data']})}\n\n"

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
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)