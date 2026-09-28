import os
import json
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional, Union

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, field_validator
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

import tracemalloc

tracemalloc.start()

load_dotenv()

import agent
from agent import TOOL_STATUS_MESSAGES, set_compiled_graph, set_execution_context, workflow


# Lifespan context manager keeps the async SQLite connection alive
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
    print("Incoming Raw Body:", body.decode("utf-8"))
    print("Validation Errors:", exc.errors())
    print("--------------------------------------\n")
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


class MediaPayload(BaseModel):
    data: str
    mime_type: str


class AgentPayload(BaseModel):
    prompt: str
    chat_id: Union[str, int]
    google_tokens: dict
    audio: Optional[MediaPayload] = None
    image: Optional[MediaPayload] = None

    @field_validator("chat_id")
    @classmethod
    def coerce_chat_id(cls, v):
        return str(v)


@app.get("/health")
def health_check():
    return {"status": "ready", "service": "otis-brain"}


# How long we'll wait on a single, isolated DB read before giving up and
# telling the caller something is wrong, instead of hanging the whole
# request (and the Telegram "typing..." indicator) forever.
STATE_LOOKUP_TIMEOUT = 15  # seconds

# How long we'll wait between agent events (tool calls, model tokens, etc)
# before sending the user a "still working" ping. Agents doing several
# sequential tool calls can legitimately go quiet for a bit; this just keeps
# the user informed instead of leaving them staring at "Thinking...".
STALL_TIMEOUT = 30  # seconds

# Absolute ceiling on total processing time for one request. If we hit this,
# we stop and tell the user, rather than hanging indefinitely with no reply
# at all — this is what was causing "sometimes no answer".
HARD_TIMEOUT = 180  # seconds


@app.post("/api/agent/dispatch-stream")
async def dispatch_prompt_stream(payload: AgentPayload):
    if agent.otis_graph is None:
        raise HTTPException(
            status_code=503, detail="Agent brain is still initializing."
        )

    tokens = payload.google_tokens
    tokens["client_id"] = os.getenv("GOOGLE_CLIENT_ID")
    tokens["client_secret"] = os.getenv("GOOGLE_CLIENT_SECRET")
    set_execution_context(tokens, payload.chat_id)

    current_time_str = datetime.now().strftime("%A, %B %d, %Y, %I:%M %p")

    system_instruction = f"""You are Otis, an elite, highly direct personal executive assistant.
Current Date and Time: {current_time_str} (IST / Asia/Kolkata, UTC+05:30).
User Timezone: Asia/Kolkata (+05:30).

TOOLING & CAPABILITY MATRIX:
- Google Workspace: Gmail (search, fetch unread, send), Calendar (list, create), Contacts (search), Tasks (list, create, complete, delete).
- Drive & Sheets: Drive file search, Sheets (read data, append rows, create sheets, clear ranges, delete rows).
- Intelligence & Media: Live web search (web_search), deep webpage reading (browse_url), web image lookup (search_image), YouTube transcripts (get_youtube_transcript, extract_video_id).
- Hyperlocal & Commute: Real-time weather (get_current_weather), turn-by-turn routing and travel duration (get_commute_and_distance).

CRITICAL BEHAVIOR RULES:
1. NEVER introduce yourself with a list of features or capabilities.
2. For greetings, acknowledge in ONE short conversational sentence.
3. When creating calendar events, use strict ISO-8601 format with the +05:30 timezone offset.
4. Execute tools immediately and report the outcome without fluff.
5. If the user shares an HTTP/HTTPS link to read, analyze, or summarize, use `browse_url` directly.
6. If the user asks to see a picture, photo, or visual diagram, invoke `search_image`.
7. Format all outputs with clean, scannable Markdown.
8. When search_image (or any tool) returns an image you want to show the user, put the image on its own line wrapped EXACTLY as [IMAGE_URL: <url>] for a hosted image, or [IMAGE_BASE64: <data>] for raw image data. Do not just mention or describe the URL in prose — the delivery system only renders images formatted this exact way.
"""

    config = {"configurable": {"thread_id": str(payload.chat_id)}}

    if payload.image:
        user_content = [
            {
                "type": "text",
                "text": (
                    payload.prompt
                    or "Analyze this image and execute any relevant tools."
                ),
            },
            {
                "type": "media",
                "mime_type": payload.image.mime_type,
                "data": payload.image.data,
            },
        ]
        new_user_message = HumanMessage(content=user_content)
    elif payload.audio:
        user_content = [
            {"type": "text", "text": payload.prompt},
            {
                "type": "media",
                "mime_type": payload.audio.mime_type,
                "data": payload.audio.data,
            },
        ]
        new_user_message = HumanMessage(content=user_content)
    else:
        new_user_message = HumanMessage(content=payload.prompt)

    # Isolated, bounded DB read. Without this timeout, SQLite lock
    # contention on the checkpointer file can hang this call forever with
    # zero bytes ever sent back to Node — which is what silently produces
    # "shows Thinking... but never answers".
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
                    timeout_msg = (
                        "⚠️ This request took longer than expected and was"
                        " stopped. Please try again, or break it into a"
                        " smaller request."
                    )
                    yield f"data: {json.dumps({'type': 'final', 'response': timeout_msg})}\n\n"
                    return

                try:
                    kind_wrapper, item = await asyncio.wait_for(
                        queue.get(), timeout=STALL_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    # No progress in a while — let the user know we're still
                    # on it instead of leaving them looking at silence.
                    yield f"data: {json.dumps({'type': 'status', 'message': '⏳ Still working on it...'})}\n\n"
                    continue

                if kind_wrapper == "done":
                    break

                if kind_wrapper == "error":
                    print(f"Error during stream: {item}")
                    err_msg = (
                        "⚠️ Otis encountered an error while processing your"
                        " request in real-time."
                    )
                    yield f"data: {json.dumps({'type': 'final', 'response': err_msg})}\n\n"
                    return

                event = item
                kind = event.get("event")

                # Emit when tool node begins executing
                if kind == "on_tool_start":
                    tool_name = event.get("name", "")
                    status = TOOL_STATUS_MESSAGES.get(
                        tool_name, f"⚙️ Running {tool_name}..."
                    )
                    yield f"data: {json.dumps({'type': 'status', 'message': status})}\n\n"

                # Capture final reply when chat model finishes
                elif kind == "on_chat_model_end":
                    output = event.get("data", {}).get("output")
                    if output and hasattr(output, "content"):
                        content = output.content
                        if isinstance(content, str) and content.strip():
                            final_reply_holder["text"] = content
                        elif isinstance(content, list):
                            texts = [
                                b.get("text", "")
                                for b in content
                                if isinstance(b, dict) and b.get("type") == "text"
                            ]
                            if texts:
                                final_reply_holder["text"] = "\n".join(texts)

            # Yield final payload
            yield f"data: {json.dumps({'type': 'final', 'response': final_reply_holder['text'].strip()})}\n\n"

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