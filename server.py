import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from agent_framework._types import Content, Message
from agent_framework._workflows._agent import WorkflowAgent

from app import build_workflow_agent, local_persistence_provider
from utils.logger import log

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

WORKFLOW_NAME = "Property_Management_Workflow"
SESSION_DB_PATH = Path(__file__).resolve().parent / "session_state.db"

with closing(sqlite3.connect(SESSION_DB_PATH)) as _conn:
    _conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            checkpoint_id TEXT,
            pending_request_id TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    _conn.commit()


def _load_session_meta(session_id: str) -> dict:
    with closing(sqlite3.connect(SESSION_DB_PATH)) as conn:
        row = conn.execute(
            "SELECT checkpoint_id, pending_request_id FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        log.debug("No session metadata found for session_id=%s", session_id)
        return {"checkpoint_id": None, "pending_request_id": None}
    log.debug(
        "Loaded session metadata for session_id=%s checkpoint_id=%s pending_request_id=%s",
        session_id,
        row[0],
        row[1],
    )
    return {"checkpoint_id": row[0], "pending_request_id": row[1]}


def _save_session_meta(session_id: str, checkpoint_id: str | None, pending_request_id: str | None) -> None:
    with closing(sqlite3.connect(SESSION_DB_PATH)) as conn:
        conn.execute(
            """
            INSERT INTO sessions (session_id, checkpoint_id, pending_request_id, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(session_id) DO UPDATE SET
                checkpoint_id = excluded.checkpoint_id,
                pending_request_id = excluded.pending_request_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (session_id, checkpoint_id, pending_request_id),
        )
        conn.commit()
    log.debug(
        "Saved session metadata for session_id=%s checkpoint_id=%s pending_request_id=%s",
        session_id,
        checkpoint_id,
        pending_request_id,
    )


async def _resolve_new_checkpoint_id(
    previous_checkpoint_id: str | None,
    checkpoint_ids_before: set,
) -> str | None:
    """Resolve the newest checkpoint created during the current run."""
    after_ids = set(await local_persistence_provider.list_checkpoint_ids(workflow_name=WORKFLOW_NAME))
    new_ids = after_ids - checkpoint_ids_before
    if not new_ids:
        return previous_checkpoint_id

    checkpoints = [await local_persistence_provider.load(checkpoint_id) for checkpoint_id in new_ids]
    latest = max(checkpoints, key=lambda checkpoint: checkpoint.timestamp)
    log.debug(
        "Resolved new checkpoint. previous=%s new=%s candidates=%d",
        previous_checkpoint_id,
        latest.checkpoint_id,
        len(new_ids),
    )
    return latest.checkpoint_id


def _extract_pending_request_id(result) -> str | None:
    """Extract the latest pending request-info call_id from workflow output."""
    messages = getattr(result, "messages", []) or []
    for message in reversed(messages):
        contents = getattr(message, "contents", []) or []
        for content in contents:
            if getattr(content, "type", None) == "function_call":
                if getattr(content, "name", None) == WorkflowAgent.REQUEST_INFO_FUNCTION_NAME:
                    call_id = getattr(content, "call_id", None)
                    return str(call_id) if call_id else None
    return None


def _build_input_messages(user_text: str, pending_request_id: str | None) -> list[Message]:
    if pending_request_id:
        # Workflow resume expects a tool-role function_result payload.
        return [
            Message(
                role="tool",
                contents=[
                    Content(
                        "function_result",
                        call_id=pending_request_id,
                        result=[Message(role="user", contents=[user_text])],
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

    try:
        log.info("Incoming chat request session_id=%s message_length=%d", session_id, len(user_text))
        meta = _load_session_meta(session_id)
        checkpoint_id = meta["checkpoint_id"]
        pending_request_id = meta["pending_request_id"]

        request_agent = build_workflow_agent()

        input_payload = _build_input_messages(user_text, pending_request_id)
        log.debug(
            "Prepared input payload session_id=%s mode=%s",
            session_id,
            "resume" if pending_request_id else "new_turn",
        )
        checkpoint_ids_before = set(
            await local_persistence_provider.list_checkpoint_ids(workflow_name=WORKFLOW_NAME)
        )

        result = await request_agent.run(
            messages=input_payload,
            checkpoint_id=checkpoint_id,
            checkpoint_storage=local_persistence_provider,
        )

        new_pending_request_id = _extract_pending_request_id(result)
        new_checkpoint_id = await _resolve_new_checkpoint_id(checkpoint_id, checkpoint_ids_before)
        _save_session_meta(session_id, new_checkpoint_id, new_pending_request_id)

        agent_reply = getattr(result, "text", str(result))
        current_agent = getattr(result, "active_agent", None) or request_agent.name or "Master_Property_Workflow"
        log.info(
            "Chat completed session_id=%s active_agent=%s checkpoint_id=%s pending_request_id=%s",
            session_id,
            current_agent,
            new_checkpoint_id,
            new_pending_request_id,
        )

        return ChatOutput(
            session_id=session_id,
            active_agent=str(current_agent),
            response=str(agent_reply)
        )

    except Exception as e:
        log.exception("Agent execution failed for session_id=%s", session_id)
        raise HTTPException(status_code=500, detail=f"Agent Execution Failure: {str(e)}")

@app.get("/health")
def health_check():
    return {"status": "healthy", "engine": "workflow_agent_loaded"}
