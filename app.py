import os
from dotenv import load_dotenv

load_dotenv()

from pathlib import Path

from agent_framework.openai import OpenAIChatClient
from agent_framework.orchestrations import HandoffBuilder
from agent_framework.devui import serve
from agent_framework import FileCheckpointStorage

from utils.agent_config import build_agent_from_config
from utils.logger import log

# --- 1. INITIALIZE CLIENT ---
chat_client = OpenAIChatClient(
    api_key=os.environ.get("OPENAI_API_KEY"),
    model=os.environ.get("AGENT_MODEL", "gpt-4o-mini")
)

# --- 2. DEFINE THE SPECIALIZED AGENTS FROM YAML CONFIG ---
property_agent = build_agent_from_config("property_finder.yaml", chat_client)
scheduler_agent = build_agent_from_config("tour_scheduler.yaml", chat_client)
portal_agent = build_agent_from_config("customer_portal.yaml", chat_client)

# --- 3. CHECKPOINT STORAGE ---
# NOTE: this must implement the actual CheckpointStorage protocol
# (save_checkpoint / load_checkpoint / list_checkpoint_ids / list_checkpoints /
# delete_checkpoint), or the workflow's pause/resume bookkeeping for
# request_info gets corrupted and you'll see errors like
# "Unexpected content type while awaiting request info responses."
# The previous ConcreteFileCheckpointStore implemented an unrelated
# read_state/write_state/topic interface, so it silently failed to
# checkpoint anything correctly.
#
# Deliberately durable (on disk), not InMemoryCheckpointStorage. The whole
# point of MAF's checkpoint_id/checkpoint_storage contract is that a
# Workflow object never has to stay alive in process memory between HTTP
# requests -- server.py builds a fresh, throwaway WorkflowAgent per request
# and restores its state from here. An in-memory store would defeat that:
# state would vanish on every restart/redeploy, same as keeping the live
# objects around in a dict.
#
# allowed_checkpoint_types: FileCheckpointStorage unpickles checkpoint
# payloads with a restricted unpickler -- by default it only allows
# primitives plus types under the `agent_framework.` package prefix (and
# openai.types). Two extra types show up in every pending request_info
# checkpoint for a HandoffBuilder workflow and must be explicitly
# allowlisted, or resuming a paused conversation after a process restart
# fails with "Checkpoint deserialization blocked for type '...'":
#
#   - "agent_framework_orchestrations._handoff:HandoffAgentUserRequest"
#     The request payload itself. Lives in the *separate* top-level package
#     `agent_framework_orchestrations` (no trailing dot after
#     "agent_framework", so it does NOT match the `agent_framework.` prefix).
#   - "types:GenericAlias"
#     ctx.request_info(HandoffAgentUserRequest(response), list[Message])
#     records the expected response type (`list[Message]`) alongside the
#     request so it can validate the resume payload later. `list[Message]`
#     is itself a `types.GenericAlias` instance, which gets pickled too.
CHECKPOINT_DIR = Path(__file__).resolve().parent / "checkpoints"
local_persistence_provider = FileCheckpointStorage(
    CHECKPOINT_DIR,
    allowed_checkpoint_types=[
        "agent_framework_orchestrations._handoff:HandoffAgentUserRequest",
        "types:GenericAlias",
    ],
)


# --- 4. BUILD THE ORCHESTRATION GRAPH ---
# IMPORTANT: HandoffBuilder workflows are inherently stateful. Every agent turn
# pauses the workflow with a `request_info` event awaiting the user's next
# message (see agent_framework_orchestrations._handoff: `ctx.request_info(...)`
# after each agent reply). That paused/resumed state lives on the *Workflow*
# instance itself (`workflow.status`), not in any AgentSession.
#
# Building ONE workflow/workflow_agent at import time and sharing it across
# every HTTP request (every session_id) means all conversations mutate the
# same underlying state machine. As soon as ANY session pauses awaiting
# request_info, the shared workflow is globally "stuck" in
# IDLE_WITH_PENDING_REQUESTS -- the very next call from *any* session (a
# brand new conversation included) gets routed into the resume path and
# blows up with "Unexpected content type while awaiting request info
# responses." This is why the failure reliably shows up on the "second
# request": the first request always pushes the shared workflow into the
# paused state.
#
# Fix: never keep a Workflow alive across requests at all. Build a fresh,
# disposable Workflow (and WorkflowAgent wrapper) on every single call, and
# let `checkpoint_id` + `checkpoint_storage` (FileCheckpointStorage above)
# restore that conversation's state into it. This is the pattern MAF's
# run() API is actually designed around -- see server.py, which calls
# build_workflow_agent() once per /v1/chat request and discards it
# afterward. The three specialist Agents below are stateless config/LLM
# wrappers and are safe to reuse across every disposable workflow instance.
def build_workflow_agent():
    """Construct a brand-new, disposable workflow agent.

    Call this on every request -- it is cheap (no I/O, no LLM calls) and
    holds no conversation state of its own. Conversation continuity comes
    entirely from passing `checkpoint_id`/`checkpoint_storage` into
    `.run()`, not from keeping this object alive.
    """
    log.info("[GRAPH BUILD] Compiling Handoff Builder Matrix...")
    builder = HandoffBuilder(
        name="Property_Management_Workflow",
        participants=[property_agent, scheduler_agent, portal_agent],
        checkpoint_storage=local_persistence_provider
    )
    builder.with_start_agent(property_agent)

    # Configure agent transition pathways
    builder.add_handoff(
        source=property_agent,
        targets=[scheduler_agent],
        description="Hand off here when the user wants to book or schedule a tour for a property."
    )
    builder.add_handoff(
        source=property_agent,
        targets=[portal_agent],
        description="Hand off here when the user wants to list, cancel, or modify an existing booking."
    )
    builder.add_handoff(
        source=scheduler_agent,
        targets=[property_agent],
        description="Hand off back to the finder agent when the user wants to look for more properties or start a new search."
    )
    builder.add_handoff(
        source=portal_agent,
        targets=[property_agent],
        description="Hand off back to the finder agent when they are done managing their existing appointments."
    )

    workflow = builder.build()

    return workflow.as_agent(
        name="Master_Property_Workflow",
        description="A master workflow agent that orchestrates the property finder, tour scheduler, and customer portal agents."
    )


# A single shared instance for the DevUI explorer (single-user, interactive
# debugging tool) -- this is fine because DevUI itself manages one workflow
# at a time. The FastAPI gateway in server.py must NOT reuse this instance;
# it calls build_workflow_agent() fresh on every request instead.
workflow_agent = build_workflow_agent()

# --- 5. EXPOSE TO THE FRAMEWORK SERVER ---
if __name__ == "__main__":
    app_port = int(os.environ.get("APP_PORT", "8000"))
    serve(
        entities=[
            property_agent,
            scheduler_agent,
            portal_agent,
            workflow_agent,
            workflow_agent.workflow
        ],
        port=app_port
    )