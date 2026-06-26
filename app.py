import os
from dotenv import load_dotenv

load_dotenv()

from agent_framework.openai import OpenAIChatClient
from agent_framework.orchestrations import HandoffBuilder
from agent_framework.devui import serve

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

# --- 3. CONCRETE STATE STORAGE BRIDGE ENGINE ---
class ConcreteFileCheckpointStore:
    """
    A concrete state storage provider that intercepts workflow metadata 
    and writes snapshots to local variables, bypassing serialization traps.
    """
    def __init__(self):
        self._cache = {}

    def read_state(self, key, *args, **kwargs):
        return self._cache.get(str(key), {})

    def write_state(self, key, state, *args, **kwargs):
        self._cache[str(key)] = state

    def get_topic(self, *args, **kwargs): return None
    def write_topic(self, *args, **kwargs): pass
    def delete_topic(self, *args, **kwargs): pass
    def list_topics(self, *args, **kwargs): return []
    def get_index_text(self, *args, **kwargs): return ""
    def rebuild_index(self, *args, **kwargs): pass
    def search_transcripts(self, *args, **kwargs): return []
    def get_transcripts_directory(self, *args, **kwargs): return ""

local_persistence_provider = ConcreteFileCheckpointStore()

# --- 4. BUILD THE ORCHESTRATION GRAPH ---
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

# FIXED: Provided a descriptive identity name to the standard wrapper interface
workflow_agent = workflow.as_agent(
    name="Master_Property_Workflow",
    description="A master workflow agent that orchestrates the property finder, tour scheduler, and customer portal agents."
)

# --- 5. EXPOSE TO THE FRAMEWORK SERVER ---
if __name__ == "__main__":
    app_port = int(os.environ.get("APP_PORT", "8000"))
    serve(
        entities=[
            property_agent, 
            scheduler_agent, 
            portal_agent,
            workflow_agent,
            workflow
        ], 
        port=app_port
    )