import os
from typing import Annotated, Sequence, TypedDict
from dotenv import load_dotenv

load_dotenv()

from langchain_core.messages import BaseMessage
from langchain_google_genai import ChatGoogleGenerativeAI 
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from tools.gmail_tools import fetch_unread_emails, search_email_threads ,send_email
from tools.calendar_tools import list_upcoming_events, create_calendar_event
from tools.contacts_tools import search_contact

# Execution context holder
_REQUEST_CONTEXT = {"tokens": {}, "chat_id": ""}

def set_execution_context(tokens: dict, chat_id: str):
    _REQUEST_CONTEXT["tokens"] = tokens
    _REQUEST_CONTEXT["chat_id"] = chat_id

def get_current_google_tokens():
    return _REQUEST_CONTEXT["tokens"]

def get_current_chat_id():
    return _REQUEST_CONTEXT["chat_id"]

# Operational Tools
TOOLS = [
    fetch_unread_emails,
    search_email_threads,
    list_upcoming_events,
    create_calendar_event,
    search_contact,
    send_email
]

# Graph State Schema
class OtisState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]

# ==========================================
# INITIALIZE GOOGLE GEN AI (GEMINI)
# ==========================================
# We use gemini-1.5-pro or gemini-1.5-flash for tool calling
llm = ChatGoogleGenerativeAI(
    model="gemini-3.1-flash-lite",
    google_api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"),
    
).bind_tools(TOOLS)

def reasoner_node(state: OtisState):
    """Brain node: inspects state, evaluates tools, returns response or calls."""
    response = llm.invoke(state["messages"])
    return {"messages": [response]}

# Assemble LangGraph
workflow = StateGraph(OtisState)

workflow.add_node("reasoner", reasoner_node)
workflow.add_node("tools", ToolNode(TOOLS))

workflow.add_edge(START, "reasoner")
workflow.add_conditional_edges("reasoner", tools_condition)
workflow.add_edge("tools", "reasoner")

otis_graph = workflow.compile()