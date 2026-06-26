import os
import uuid
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from agent_framework._types import Content, Message
from agent_framework._workflows._agent import WorkflowAgent

# Import the named workflow agent wrapper from app
from app import workflow_agent

app = FastAPI(
    title="Elite Real Estate Multi-Agent API Gateway",
    description="Production-grade headless API driving the workflow via a unified agent wrapper contract.",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Holds session state for each conversation
SESSION_STORAGE = {}


def _extract_pending_request_id(result) -> str | None:
    messages = getattr(result, "messages", []) or []
    for message in messages:
        contents = getattr(message, "contents", []) or []
        for content in contents:
            if getattr(content, "type", None) == "function_call":
                if getattr(content, "name", None) == WorkflowAgent.REQUEST_INFO_FUNCTION_NAME:
                    return str(getattr(content, "call_id", "")) or None
    return None


def _build_input_messages(user_text: str, pending_request_id: str | None) -> list[Message]:
    if pending_request_id:
        request_response = [Message(role="user", contents=[user_text])]
        return [
            Message(
                role="user",
                contents=[
                    Content(
                        type="function_result",
                        call_id=pending_request_id,
                        result=request_response,
                    )
                ],
            )
        ]

    return [Message(role="user", contents=[user_text])]

class ChatInput(BaseModel):
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    message: str

class ChatOutput(BaseModel):
    session_id: str
    active_agent: str
    response: str

@app.post("/v1/chat", response_model=ChatOutput)
async def handle_agent_chat(payload: ChatInput):
    session_id = payload.session_id
    user_text = payload.message.strip()
    
    if not user_text:
        raise HTTPException(status_code=400, detail="Message content cannot be empty.")
        
    # Initialize a native framework state container session for new conversations
    if session_id not in SESSION_STORAGE:
        SESSION_STORAGE[session_id] = {
            "session": workflow_agent.create_session(session_id=session_id),
            "pending_request_id": None,
        }
        
    try:
        state = SESSION_STORAGE[session_id]
        # Backward compatibility if an old session object is still present in memory.
        if not isinstance(state, dict):
            state = {"session": state, "pending_request_id": None}
            SESSION_STORAGE[session_id] = state

        active_session = state["session"]
        pending_request_id = state.get("pending_request_id")

        input_payload = _build_input_messages(user_text, pending_request_id)

        result = await workflow_agent.run(messages=input_payload, session=active_session)

        state["pending_request_id"] = _extract_pending_request_id(result)
        
        # Read the clean string directly via the official response text property
        agent_reply = getattr(result, "text", str(result))
            
        # Extract metadata or default cleanly to master runtime wrapper name
        current_agent = getattr(result, "active_agent", None) or workflow_agent.name or "Master_Property_Workflow"
        
        return ChatOutput(
            session_id=session_id,
            active_agent=str(current_agent),
            response=str(agent_reply)
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Agent Execution Failure: {str(e)}")

@app.get("/health")
def health_check():
    return {"status": "healthy", "engine": "workflow_agent_loaded"}