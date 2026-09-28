import os
from typing import Annotated, Sequence, TypedDict
from dotenv import load_dotenv

load_dotenv()

from langchain_core.messages import BaseMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import StateGraph, START
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from tools.gmail_tools import fetch_unread_emails, search_email_threads, send_email
from tools.calendar_tools import list_upcoming_events, create_calendar_event,delete_calendar_event,update_calendar_event
from tools.contacts_tools import search_contact
from tools.task_tools import list_tasks, create_task, complete_task, delete_task
from tools.youtube_tools import extract_video_id, get_youtube_transcript
from tools.weather_tools import get_current_weather
from tools.maps_tools import get_commute_and_distance
from tools.search_tools import web_search,search_image,browse_url
from tools.drive_tools import search_drive_files
from tools.sheet_tools import create_spreadsheet ,append_row_to_sheet ,read_sheet_data,clear_sheet_range,delete_sheet_row

# Execution context holder
_REQUEST_CONTEXT = {"tokens": {}, "chat_id": ""}

def set_execution_context(tokens: dict, chat_id: str):
    _REQUEST_CONTEXT["tokens"] = tokens
    _REQUEST_CONTEXT["chat_id"] = chat_id

def get_current_google_tokens():
    return _REQUEST_CONTEXT["tokens"]

def get_current_chat_id():
    return _REQUEST_CONTEXT["chat_id"]

TOOLS = [
    fetch_unread_emails,
    search_email_threads,
    list_upcoming_events,
    create_calendar_event,
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
    clear_sheet_range,delete_sheet_row,
    delete_calendar_event,
    update_calendar_event
]

TOOL_STATUS_MESSAGES = {
    # Gmail
    "fetch_unread_emails": "📬 Checking your unread emails...",
    "search_email_threads": "📨 Searching email threads...",
    "send_email": "✉️ Sending email...",
    
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
    
    # Google Drive
    "search_drive_files": "📁 Searching your Google Drive...",
    
    # Google Sheets
    "read_sheet_data": "📊 Reading spreadsheet data...",
    "append_row_to_sheet": "📝 Writing row to spreadsheet...",
    "create_spreadsheet": "📊 Creating new Google Sheet...",
    "clear_sheet_range": "🧹 Clearing range in spreadsheet...",
    "delete_sheet_row": "🗑️ Deleting row from spreadsheet...",
}


class OtisState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]

llm = ChatGoogleGenerativeAI(
    model="gemini-3.1-flash-lite",
    google_api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"),
).bind_tools(TOOLS)

async def reasoner_node(state: OtisState):
    response = await llm.ainvoke(state["messages"])
    return {"messages": [response]}


# Assemble LangGraph
workflow = StateGraph(OtisState)

workflow.add_node("reasoner", reasoner_node)
workflow.add_node("tools", ToolNode(TOOLS))

workflow.add_edge(START, "reasoner")
workflow.add_conditional_edges("reasoner", tools_condition)
workflow.add_edge("tools", "reasoner")

# Variable to hold the compiled graph with async checkpointer
otis_graph = None

def set_compiled_graph(graph):
    global otis_graph
    otis_graph = graph