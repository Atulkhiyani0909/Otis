import os
import json
import asyncio
import tracemalloc
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional, Union

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

def build_google_tokens(raw: dict) -> dict:
    tokens = dict(raw or {})
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

    try:
        if not creds.valid or creds.expired:
            creds.refresh(GoogleAuthRequest())
    except RefreshError as e:
        print(f"[AUTH WARNING] Refresh token rejected: {e}")
        return tokens

    tokens.update({
        "token": creds.token,
        "access_token": creds.token,
        "refresh_token": refresh_token,
        "token_uri": token_uri,
        "client_id": client_id,
        "client_secret": client_secret,
    })
    return tokens

async def resolve_google_tokens(raw: dict) -> dict:
    try:
        return await asyncio.to_thread(build_google_tokens, raw)
    except Exception as e:
        print(f"[AUTH REFRESH ERROR] {e}")
        return raw or {}

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
    google_tokens: dict = {}
    audio: Optional[MediaPayload] = None
    image: Optional[MediaPayload] = None
    location: Optional[LocationPayload] = None

    @field_validator("chat_id")
    @classmethod
    def coerce_chat_id(cls, v):
        return str(v)

class ConfirmActionPayload(BaseModel):
    chat_id: Union[str, int]
    google_tokens: dict
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
        )
        return {"result": result}

    return JSONResponse(status_code=400, content={"error": f"Unknown action: {action}"})

STATE_LOOKUP_TIMEOUT = 15
STALL_TIMEOUT = 30
HARD_TIMEOUT = 180

@app.post("/api/agent/dispatch-stream")
async def dispatch_prompt_stream(payload: AgentPayload):
    if agent.otis_graph is None:
        raise HTTPException(status_code=503, detail="Agent brain is still initializing.")

    tokens = await resolve_google_tokens(payload.google_tokens)
    set_execution_context(tokens, payload.chat_id)

    current_time_str = datetime.now().strftime("%A, %B %d, %Y, %I:%M %p")

    system_instruction = f"""You are Otis, an elite, highly direct autonomous executive assistant.
Current Date and Time: {current_time_str} (IST / Asia/Kolkata, UTC+05:30).
User Timezone: Asia/Kolkata (+05:30).

TOOLING & CAPABILITY MATRIX:
• Google Workspace: Gmail (search_email_threads, fetch_unread_emails, send_email), Calendar (list_upcoming_events, create_calendar_event, update_calendar_event, delete_calendar_event), Contacts (search_contact), Tasks (list_tasks, create_task, complete_task, delete_task).
• Drive & Sheets: Drive (search_drive_files), Sheets (read_sheet_data, append_row_to_sheet, create_spreadsheet, clear_sheet_range, delete_sheet_row).
• Intelligence & Media: web_search, browse_url, search_image, generate_image, get_youtube_transcript, extract_video_id.
• Navigation & Environment: get_current_weather, get_commute_and_distance.

NON-NEGOTIABLE OPERATIONAL RULES:
1. NO GUESSING OR HALLUCINATED ACTIONS:
- NEVER guess document IDs, event IDs, task IDs, or contact email addresses.
- To cancel/update a meeting: Call `list_upcoming_events` first to get the verified `event_id`, then invoke `update_calendar_event` or `delete_calendar_event`.
- To complete/delete a task: Call `list_tasks` first to get the `task_id`, then proceed.
- To email someone by name: Call `search_contact` first to locate their exact email address before staging.

2. DRIVE & WORKSPACE SEARCH INTEGRITY:
- If the user mentions any file, document, PDF, invoice, resume, or sheet, YOU MUST immediately call `search_drive_files`. Never answer "I do not see the file" without searching Drive first.

3. EMAIL DISPATCH SAFETY (HUMAN-IN-THE-LOOP):
- Always invoke `send_email` with the explicit recipient, subject, and drafted body.
- `send_email` automatically stages the draft for approval. Never claim an email was dispatched until confirmed.

4. STRICT ISO-8601 TIMESTAMPS:
- For calendar tools (`create_calendar_event`, `update_calendar_event`), start and end times must strictly follow ISO-8601 with offset: `YYYY-MM-DDTHH:MM:SS+05:30`.
- Assume meetings are 30 minutes long unless specified otherwise.

5. ALWAYS PRODUCE A TEXT RESPONSE:
- After a tool executes, you MUST synthesize a clear, informative message to the user summarizing the result.

6. MEDIA FORMATTING DIRECTIVES:
- If you call `search_image` to find a picture, output the image tag on its own line: [IMAGE_URL: <url>].
- If you call `generate_image`, output the base64 result on its own line: [IMAGE_BASE64: <data>].

7. OUTPUT STYLE & PERSONALITY:
- Be concise, sharp, and executive. Zero conversational fluff.
- Use standard, clean Telegram Markdown (bold `**text**`, bullet points `•`).
"""

    config = {"configurable": {"thread_id": str(payload.chat_id)}}

    # Inject location context if sent
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
                    status = TOOL_STATUS_MESSAGES.get(tool_name, f"⚙️️ Running {tool_name}...")
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