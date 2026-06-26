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

# Import the factory and the durable checkpoint store, NOT a shared
# singleton workflow. See app.py for the full explanation: HandoffBuilder
# workflows pause via request_info after every single turn, so the
# pause/resume state has to live somewhere durable (FileCheckpointStorage),
# not on a long-lived Python object kept around per request/session.
from app import build_workflow_agent, local_persistence_provider

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

# --- LIGHTWEIGHT SESSION METADATA ---
# Only two small strings per conversation are kept here: the last
# checkpoint_id and any pending_request_id. Everything else (the actual
# conversation state, in-flight tool calls, etc.) lives in
# local_persistence_provider (FileCheckpointStorage, on disk) and is
# restored into a fresh, disposable WorkflowAgent on every request -- see
# handle_agent_chat below. SQLite (not a process-memory dict) so this
# metadata survives restarts too, and writes don't corrupt under concurrent
# requests the way a hand-rolled JSON file read/write would.
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
        return {"checkpoint_id": None, "pending_request_id": None}
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


async def _resolve_new_checkpoint_id(
    previous_checkpoint_id: str | None,
    checkpoint_ids_before: set,
) -> str | None:
    """Figure out which checkpoint this request's run() call just created.

    A single .run() call can create more than one checkpoint (one per
    internal superstep). We diff the checkpoint IDs that exist now against
    the snapshot taken before this run to scope the search to *this* run's
    checkpoints, then take the one with the latest timestamp as the tip.

    We can't walk the previous_checkpoint_id chain instead: this workflow's
    multiple executors each write their own checkpoint per superstep, and
    several of those checkpoints can legitimately share the same
    previous_checkpoint_id (including None, for parallel branches). A
    parent->child dict built from that field collapses those collisions and
    silently drops some checkpoints, so the walk can stop short of the real
    tip and leave a stale pending_request_id in session metadata.
    """
    after_ids = set(await local_persistence_provider.list_checkpoint_ids(workflow_name=WORKFLOW_NAME))
    new_ids = after_ids - checkpoint_ids_before
    if not new_ids:
        return previous_checkpoint_id

    checkpoints = [await local_persistence_provider.load(checkpoint_id) for checkpoint_id in new_ids]
    latest = max(checkpoints, key=lambda checkpoint: checkpoint.timestamp)
    return latest.checkpoint_id


def _extract_pending_request_id(result) -> str | None:
    """
    Scan the most recent messages first so a stale, already-answered
    function_call from earlier in the conversation can't be picked up
    instead of the current pending request.
    """
    messages = getattr(result, "messages", []) or []
    for message in reversed(messages):
        contents = getattr(message, "contents", []) or []
        for content in contents:
            if getattr(content, "type", None) == "function_call":
                if getattr(content, "name", None) == WorkflowAgent.REQUEST_INFO_FUNCTION_NAME:
                    call_id = getattr(content, "call_id", None)
                    # Guard against str(None) -> "None" (a truthy string)
                    # which would make _build_input_messages think a
                    # request is pending when it actually isn't.
                    return str(call_id) if call_id else None
    return None


def _build_input_messages(user_text: str, pending_request_id: str | None) -> list[Message]:
    if pending_request_id:
        # IMPORTANT: when resuming a paused workflow that is awaiting a
        # request_info response, the function_result content must be
        # sent on a message with role="tool", not role="user".
        # The workflow's request-info dispatcher only recognizes
        # function_result content arriving on a "tool"-role message;
        # sending it as "user" triggers:
        #   "Unexpected content type while awaiting request info responses."
        #
        # Just as important: this must be the ONLY message in the list. The
        # framework's `_extract_function_responses` walks every content item
        # of every input message and raises the same error if it finds
        # anything other than a function_result/function_approval_response.
        # We must never let prior turns' plain-text messages ride along with
        # the resume payload -- that's why this gateway never attaches an
        # AgentSession/history provider to the workflow agent. Conversation
        # continuity comes from checkpoint_id/checkpoint_storage restoring
        # the workflow's own internal state, not from replayed messages.
        #
        # HandoffBuilder's request_info always records list[Message] as the
        # expected response type (see app.py). WorkflowAgent reads the
        # response straight off `content.result`, so we must construct the
        # Content directly with result=[Message(...)] -- going through
        # Content.from_function_result() would coerce it down to the
        # concatenated text string and fail the workflow's type check.
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
        meta = _load_session_meta(session_id)
        checkpoint_id = meta["checkpoint_id"]
        pending_request_id = meta["pending_request_id"]

        # Disposable per request: holds no state of its own. Conversation
        # continuity comes entirely from restoring `checkpoint_id` below,
        # not from keeping this object (or any session=AgentSession) alive
        # between requests. This is also what makes the gateway safe to run
        # with multiple uvicorn workers / replicas -- no in-process state to
        # desync across processes.
        request_agent = build_workflow_agent()

        input_payload = _build_input_messages(user_text, pending_request_id)
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

        # Read the clean string directly via the official response text property
        agent_reply = getattr(result, "text", str(result))

        # Extract metadata or default cleanly to master runtime wrapper name
        current_agent = getattr(result, "active_agent", None) or request_agent.name or "Master_Property_Workflow"

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
