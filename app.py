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

log.info("Initializing chat client and specialist agents")

chat_client = OpenAIChatClient(
    api_key=os.environ.get("OPENAI_API_KEY"),
    model=os.environ.get("AGENT_MODEL", "gpt-4o-mini")
)

property_agent = build_agent_from_config("property_finder.yaml", chat_client)
scheduler_agent = build_agent_from_config("tour_scheduler.yaml", chat_client)
portal_agent = build_agent_from_config("customer_portal.yaml", chat_client)

# Durable checkpointing is required for request/response resume across requests and restarts.
CHECKPOINT_DIR = Path(__file__).resolve().parent / "checkpoints"
local_persistence_provider = FileCheckpointStorage(
    CHECKPOINT_DIR,
    allowed_checkpoint_types=[
        "agent_framework_orchestrations._handoff:HandoffAgentUserRequest",
        "types:GenericAlias",
    ],
)

def build_workflow_agent():
    """Construct a disposable workflow agent used per request in the API layer."""
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
    log.debug("Workflow built successfully with participants: %s", [
        property_agent.name,
        scheduler_agent.name,
        portal_agent.name,
    ])

    return workflow.as_agent(
        name="Master_Property_Workflow",
        description="A master workflow agent that orchestrates the property finder, tour scheduler, and customer portal agents."
    )


# Shared only for DevUI. FastAPI creates a fresh workflow per request.
workflow_agent = build_workflow_agent()

if __name__ == "__main__":
    app_port = int(os.environ.get("APP_PORT", "8000"))
    log.info("Starting DevUI server on port=%d", app_port)
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