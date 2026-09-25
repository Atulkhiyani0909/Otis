import os
from datetime import datetime
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from typing import Optional, Dict, Any, Union
from pydantic import BaseModel, field_validator

load_dotenv()

from agent import otis_graph, set_execution_context
from langchain_core.messages import SystemMessage, HumanMessage

app = FastAPI(title="Otis AI Brain Service")



@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    body = await request.body()
    print("\n--- [422 VALIDATION ERROR DETAILS] ---")
    print("Incoming Raw Body:", body.decode("utf-8"))
    print("Validation Errors:", exc.errors())
    print("--------------------------------------\n")
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

class AgentPayload(BaseModel):
    prompt: str
    chat_id: Union[str, int]
    google_tokens: dict

    @field_validator("chat_id")
    @classmethod
    def coerce_chat_id(cls, v):
        return str(v)

@app.get("/health")
def health_check():
    return {"status": "ready", "service": "otis-brain"}

@app.post("/api/agent/dispatch")
async def dispatch_prompt(payload: AgentPayload):
    # Set request tokens for tools to access
    tokens = payload.google_tokens
    tokens["client_id"] = os.getenv("GOOGLE_CLIENT_ID")
    tokens["client_secret"] = os.getenv("GOOGLE_CLIENT_SECRET")
    set_execution_context(tokens, payload.chat_id)

   

    current_time_str = datetime.now().strftime("%A, %B %d, %Y, %I:%M %p")

    system_instruction = f"""You are Otis, an elite, highly direct personal executive assistant.
Current Date and Time: {current_time_str} (IST / Asia/Kolkata, UTC+05:30).
User Timezone: Asia/Kolkata (+05:30).

CRITICAL BEHAVIOR RULES:
1. NEVER introduce yourself with a list of features or capabilities.
2. For greetings, reply in ONE short conversational sentence.
3. When creating calendar events, use ISO-8601 format with the +05:30 timezone offset (e.g. 2026-09-26T16:00:00+05:30).
4. Execute tools immediately and report the outcome without fluff.
"""

    try:
        final_state = await otis_graph.ainvoke({
            "messages": [
                SystemMessage(content=system_instruction),
                HumanMessage(content=payload.prompt)
            ]
        })

        last_message = final_state["messages"][-1]
        raw_content = last_message.content

    # Normalize response to a pure string:
        if isinstance(raw_content, str):
            reply = raw_content
        elif isinstance(raw_content, list):
        # Extract and join all text blocks, ignoring signatures/extras
            text_parts = [
            block.get("text", "") 
            for block in raw_content 
            if isinstance(block, dict) and block.get("type") == "text"
            ]
            reply = "\n".join(text_parts) if text_parts else str(raw_content)
        else:
            reply = str(raw_content)

        return {"response": reply.strip()}
    except Exception as e:
        print(f"Error in LangGraph execution: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)