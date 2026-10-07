import os
import contextvars
from typing import Annotated, Sequence, TypedDict, Dict, Any
from dotenv import load_dotenv

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
from tools.drive_tools import search_drive_files , read_drive_file
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
    read_drive_file
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
}


class OtisState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]


# ---------------------------------------------------------------------------
# Context budget (tune these)
# ---------------------------------------------------------------------------
MAX_CONTEXT_TOKENS = 6000      # max tokens sent to the LLM per call
MAX_TOOL_OUTPUT_CHARS = 3000   # cap on any single tool result sent to the LLM


def approx_token_counter(messages) -> int:
    """Fast local estimate (~4 chars per token). No API call, so no extra quota use."""
    if isinstance(messages, BaseMessage):
        messages = [messages]
    total = 0
    for m in messages:
        total += len(str(m.content)) // 4 + 4
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


def build_llm_context(messages):
    """Keep the system prompt + the most recent messages that fit the budget."""
    messages = shrink_tool_outputs(list(messages))
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
    if not trimmed:  # safety net: at least send the latest user message
        last_human = next((m for m in reversed(messages) if m.type == "human"), None)
        trimmed = [last_human] if last_human else messages[-1:]
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