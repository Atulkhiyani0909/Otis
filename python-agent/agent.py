import os
import contextvars
from typing import Annotated, Sequence, TypedDict, Dict, Any
from dotenv import load_dotenv
import re
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from langchain_core.tools import tool

load_dotenv()

from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    ToolMessage,
    trim_messages,
)
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import StateGraph, START
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from tools.gmail_tools import fetch_unread_emails, search_email_threads, send_email, execute_send_email_direct
from tools.calendar_tools import (
    list_upcoming_events,
    create_calendar_event,
    delete_calendar_event,
    update_calendar_event,
)
from tools.contacts_tools import search_contact
from tools.task_tools import list_tasks, create_task, complete_task, delete_task
from tools.youtube_tools import extract_video_id, get_youtube_transcript
from tools.weather_tools import get_current_weather
from tools.maps_tools import get_commute_and_distance
from tools.search_tools import web_search, search_image, browse_url, generate_image
from tools.drive_tools import search_drive_files, read_drive_file
from tools.sheet_tools import (
    create_spreadsheet,
    append_row_to_sheet,
    read_sheet_data,
    clear_sheet_range,
    delete_sheet_row,
)

# Thread-safe execution context holder
_REQUEST_TOKENS: contextvars.ContextVar[Dict[str, Any]] = contextvars.ContextVar(
    "_REQUEST_TOKENS", default={}
)
_REQUEST_CHAT_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_REQUEST_CHAT_ID", default=""
)


def set_execution_context(tokens: dict, chat_id: str):
    _REQUEST_TOKENS.set(tokens)
    _REQUEST_CHAT_ID.set(str(chat_id))


def get_current_google_tokens() -> dict:
    return _REQUEST_TOKENS.get()


def get_current_chat_id() -> str:
    return _REQUEST_CHAT_ID.get()


TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata")
_CRON_FIELD = re.compile(r"^[\w\*/,\-]+$")


@tool
def set_reminder(message: str, remind_at: str = "", cron: str = "", run_agent: bool = False) -> str:
    """Schedule a reminder, alarm, or recurring task for the user.

    Args:
        message: For a plain reminder, a short text for the user (e.g. "Call Rahul").
                 If run_agent is true, an instruction for yourself (e.g. "Fetch my unread emails and summarize them").
        remind_at: ONE-TIME reminder. ISO-8601 with offset, e.g. 2026-10-09T17:00:00+05:30. Leave empty if using cron.
        cron: RECURRING schedule, 5 fields: minute hour day-of-month month day-of-week, in IST.
              Example: "0 9 * * 1-5" = 9:00 AM every weekday. Leave empty for one-time reminders.
        run_agent: true if at that time you should DO something (check mail, weather, tasks) instead of just notifying.
    """
    tz = ZoneInfo(TIMEZONE)
    message = (message or "").strip()
    if not message:
        return "Error: message is empty."

    spec = {"message": message[:500], "run_agent": bool(run_agent)}
    cron = (cron or "").strip()
    remind_at = (remind_at or "").strip()

    if cron:
        parts = cron.split()
        if len(parts) != 5 or not all(_CRON_FIELD.match(p) for p in parts):
            return "Error: cron must have exactly 5 fields (minute hour day month weekday)."
        spec["cron"] = cron
        when = f"repeating ({cron}, {TIMEZONE})"
    elif remind_at:
        try:
            dt = datetime.fromisoformat(remind_at.replace("Z", "+00:00"))
        except ValueError:
            return "Error: remind_at must be ISO-8601, e.g. 2026-10-09T17:00:00+05:30."
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        dt = dt.astimezone(tz)
        if dt <= datetime.now(tz):
            return "Error: that time is already in the past. Recompute it from the current time, or ask the user."
        spec["remind_at"] = dt.isoformat()
        when = dt.strftime("%A, %d %B %Y, %I:%M %p")
    else:
        return "Error: provide either remind_at (one-time) or cron (recurring)."

    return f"Reminder scheduled for {when}. [REMINDER: {json.dumps(spec)}]"

# Autonomous LangGraph tools (execute_send_email_direct is executed ONLY upon Telegram button confirmation)
TOOLS = [
    fetch_unread_emails,
    search_email_threads,
    list_upcoming_events,
    create_calendar_event,
    update_calendar_event,
    delete_calendar_event,
    search_contact,
    send_email,
    list_tasks,
    create_task,
    complete_task,
    delete_task,
    get_current_weather,
    get_youtube_transcript,
    extract_video_id,
    get_commute_and_distance,
    web_search,
    search_image,
    browse_url,
    search_drive_files,
    read_sheet_data,
    append_row_to_sheet,
    create_spreadsheet,
    clear_sheet_range,
    delete_sheet_row,
    generate_image,
    read_drive_file,
    set_reminder
]

TOOL_STATUS_MESSAGES = {
    # Gmail
    "fetch_unread_emails": "📬 Checking your unread emails...",
    "search_email_threads": "📨 Searching email threads...",
    "send_email": "✉️ Staging email draft for approval...",
    # Calendar
    "list_upcoming_events": "📅 Checking your Google Calendar...",
    "create_calendar_event": "🗓️ Scheduling event on Google Calendar...",
    "update_calendar_event": "🗓️ Rescheduling / updating calendar event...",
    "delete_calendar_event": "🗑️ Removing event from Google Calendar...",
    # Contacts
    "search_contact": "👥 Looking up contact details...",
    # Tasks
    "list_tasks": "✅ Retrieving your task list...",
    "create_task": "📝 Adding task to your list...",
    "complete_task": "✔️ Marking task as complete...",
    "delete_task": "🗑️ Deleting task...",
    # Weather & Maps
    "get_current_weather": "⛅ Checking live weather...",
    "get_commute_and_distance": "🚗 Calculating travel route and commute...",
    # YouTube
    "get_youtube_transcript": "🎥 Extracting YouTube video transcript...",
    "extract_video_id": "🔍 Parsing YouTube video link...",
    # Search & Web Intelligence
    "web_search": "🔍 Searching the web...",
    "search_image": "📸 Searching for relevant image...",
    "browse_url": "🌐 Reading and analyzing webpage...",
    "generate_image": "🎨 Generating image...",
    # Google Drive
    "search_drive_files": "📁 Searching your Google Drive...",
    "read_drive_file": "📄 Reading file content from Drive...",
    # Google Sheets
    "read_sheet_data": "📊 Reading spreadsheet data...",
    "append_row_to_sheet": "📝 Writing row to spreadsheet...",
    "create_spreadsheet": "📊 Creating new Google Sheet...",
    "clear_sheet_range": "🧹 Clearing range in spreadsheet...",
    "delete_sheet_row": "🗑️ Deleting row from spreadsheet...",
    "set_reminder": "⏰ Setting your reminder...",
}


class OtisState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]


# ---------------------------------------------------------------------------
# Context budget (tune these)
# ---------------------------------------------------------------------------
MAX_CONTEXT_TOKENS = 6000      # max tokens sent to the LLM per call
MAX_TOOL_OUTPUT_CHARS = 3000   # cap on any single tool result sent to the LLM

# Flat token cost for media parts. Base64 length must NEVER be counted as text,
# otherwise a single photo/voice note blows the budget and gets trimmed away,
# leaving Gemini with only a system prompt ("contents are required").
IMAGE_TOKEN_COST = 1000
AUDIO_TOKEN_COST = 1500
OTHER_PART_TOKEN_COST = 200


def _content_tokens(content) -> int:
    if isinstance(content, str):
        return len(content) // 4
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, str):
                total += len(part) // 4
            elif isinstance(part, dict):
                ptype = part.get("type")
                if ptype == "text":
                    total += len(part.get("text", "")) // 4
                elif ptype == "image_url":
                    total += IMAGE_TOKEN_COST
                elif ptype == "media":
                    mime = str(part.get("mime_type", ""))
                    total += AUDIO_TOKEN_COST if mime.startswith("audio") else IMAGE_TOKEN_COST
                else:
                    total += OTHER_PART_TOKEN_COST
        return total
    return len(str(content)) // 4


def approx_token_counter(messages) -> int:
    """Fast local estimate (~4 chars per token) that treats media as a fixed cost."""
    if isinstance(messages, BaseMessage):
        messages = [messages]
    total = 0
    for m in messages:
        total += _content_tokens(m.content) + 4
        if getattr(m, "tool_calls", None):
            total += len(str(m.tool_calls)) // 4
    return total


def shrink_tool_outputs(messages):
    """Truncate huge tool results (emails, web pages, sheets) without mutating saved state."""
    out = []
    for m in messages:
        if isinstance(m, ToolMessage) and len(str(m.content)) > MAX_TOOL_OUTPUT_CHARS:
            text = str(m.content)[:MAX_TOOL_OUTPUT_CHARS] + "\n...[truncated]"
            m = m.model_copy(update={"content": text})
        out.append(m)
    return out


def strip_old_media(messages):
    """Only the most recent human message keeps its media; older ones become text-only.
    (Does not mutate saved state.)"""
    last_human_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].type == "human":
            last_human_idx = i
            break

    out = []
    for i, m in enumerate(messages):
        if m.type == "human" and isinstance(m.content, list) and i != last_human_idx:
            texts = []
            for part in m.content:
                if isinstance(part, str):
                    texts.append(part)
                elif isinstance(part, dict) and part.get("type") == "text":
                    texts.append(part.get("text", ""))
            text = " ".join(t for t in texts if t).strip() or "(attachment)"
            m = m.model_copy(update={"content": f"{text}\n[attachment removed from history]"})
        out.append(m)
    return out


def build_llm_context(messages):
    """Keep the system prompt + the most recent messages that fit the budget."""
    messages = strip_old_media(shrink_tool_outputs(list(messages)))
    trimmed = trim_messages(
        messages,
        max_tokens=MAX_CONTEXT_TOKENS,
        token_counter=approx_token_counter,
        strategy="last",
        include_system=True,       # always keep the system prompt
        start_on="human",          # never start mid tool-call (Gemini errors on orphaned tool results)
        end_on=("human", "tool"),
        allow_partial=False,
    )

    # Safety net: Gemini needs at least one non-system message ("contents are required").
    if not any(m.type != "system" for m in trimmed):
        system_msgs = [m for m in messages if m.type == "system"][:1]
        last_human = next((m for m in reversed(messages) if m.type == "human"), None)
        if last_human is not None:
            trimmed = system_msgs + [last_human]
        else:
            trimmed = system_msgs + messages[-1:]
    return trimmed


llm = ChatGoogleGenerativeAI(
    model="gemini-3.5-flash-lite",
    google_api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"),
    max_retries=4,   # backs off automatically on 429 / RESOURCE_EXHAUSTED
    timeout=60,
).bind_tools(TOOLS)


async def reasoner_node(state: OtisState):
    context = build_llm_context(state["messages"])
    response = await llm.ainvoke(context)

    # Check if content has non-empty text
    has_text = False
    if isinstance(response.content, str) and response.content.strip():
        has_text = True
    elif isinstance(response.content, list) and any(
        isinstance(b, dict) and b.get("text", "").strip() for b in response.content
    ):
        has_text = True

    # Model ran tools but gave no text: ask for a final summary (using the SAME trimmed context)
    if not response.tool_calls and not has_text:
        if context and getattr(context[-1], "type", "") == "tool":
            follow_up = await llm.ainvoke(
                context
                + [
                    response,
                    HumanMessage(
                        content="Summarize the tool output above and give a direct, final answer to the user."
                    ),
                ]
            )
            return {"messages": [follow_up]}

    return {"messages": [response]}


# Assemble LangGraph
workflow = StateGraph(OtisState)

workflow.add_node("reasoner", reasoner_node)
workflow.add_node("tools", ToolNode(TOOLS))

workflow.add_edge(START, "reasoner")
workflow.add_conditional_edges("reasoner", tools_condition)
workflow.add_edge("tools", "reasoner")

otis_graph = None


def set_compiled_graph(graph):
    global otis_graph
    otis_graph = graph