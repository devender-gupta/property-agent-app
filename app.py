import os
from dotenv import load_dotenv

load_dotenv()

from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from agent_framework.orchestrations import HandoffBuilder
from agent_framework.devui import serve

# Import database-connected tools
from tools.property_tools import search_properties_api
from tools.scheduling_tools import (
    confirm_tour_booking, 
    list_customer_bookings_api,
    cancel_tour_booking_api,
    reschedule_tour_booking_api
)
from utils.logger import log

# --- 1. INITIALIZE CLIENT ---
chat_client = OpenAIChatClient(
    api_key=os.environ.get("OPENAI_API_KEY"),
    model=os.environ.get("AGENT_MODEL", "gpt-4o-mini")
)

# --- 2. DEFINE THE SPECIALIZED AGENTS ---

# Property Discovery Agent
property_agent = Agent(
    name="PropertyFinderAgent",
    client=chat_client,
    tools=[search_properties_api],
    require_per_service_call_history_persistence=True,
    instructions="""
    You are an elite Real Estate AI Concierge. Help users find their dream property.
    
    Guidelines:
    1. Use search_properties_api to filter options based on budget, beds, style, or amenities.
    2. If the user indicates they want to book a tour, schedule a visit, or view a specific property, gracefully hand control over to TourSchedulerAgent.
    3. If the user asks to see their existing bookings, cancel a booking, or modify an appointment, hand control over to CustomerPortalAgent.
    """
)

# Transactional Tour Booking Agent
scheduler_agent = Agent(
    name="TourSchedulerAgent",
    client=chat_client,
    tools=[confirm_tour_booking],
    require_per_service_call_history_persistence=True,
    instructions="""
    You are a Scheduling Coordinator. Your unique goal is to capture form details for a NEW property viewing tour.
    
    CRITICAL PROTOCOL FOR PROPERTY_ID:
    - Look back at the conversation history to find the name or ID of the property the user said they liked (e.g., Property 1022, Skyline Towers, etc.). 
    - You MUST use that specific identifier as the 'property_id' when calling `confirm_tour_booking`.
    
    You MUST collect exactly these 4 pieces of information from the user:
    1. Full Name
    2. Email Address
    3. Phone Number
    4. Preferred Date and Time for the tour
    
    Guidelines:
    - Ask for missing items one by one.
    - Once you have the property identity AND these 4 missing pieces, immediately execute the `confirm_tour_booking` tool.
    - After a SUCCESS confirmation, let the user know their tour is secured and hand control back to PropertyFinderAgent.
    """
)

# Customer Portal Management Agent
portal_agent = Agent(
    name="CustomerPortalAgent",
    client=chat_client,
    tools=[list_customer_bookings_api, cancel_tour_booking_api, reschedule_tour_booking_api],
    require_per_service_call_history_persistence=True,
    instructions="""
    You are the Customer Portal Representative. Your primary goal is to help users review, cancel, or reschedule existing appointments.
    
    Capabilities & Protocols:
    1. LIST BOOKINGS: Ask for their email address if you don't have it, then run `list_customer_bookings_api`.
    2. CANCEL TOUR: Pass the specific Booking UUID to `cancel_tour_booking_api`.
    3. RESCHEDULE TOUR: Pass the Booking UUID and new time to `reschedule_tour_booking_api`.
    - Once actions are finished, tell the user and hand control back to PropertyFinderAgent.
    """
)

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
        # Cleanly store the tracking references without triggering raw JSON serialization faults
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
    participants=[property_agent, scheduler_agent, portal_agent],
    checkpoint_storage=local_persistence_provider  # <--- FIXED: Prevents serialization faults across boundaries
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
workflow_agent = workflow.as_agent(name="Master_Property_Workflow")

# --- 5. EXPOSE TO THE FRAMEWORK SERVER ---
if __name__ == "__main__":
    app_port = int(os.environ.get("APP_PORT", "8000"))
    serve(
        entities=[
            property_agent, 
            scheduler_agent, 
            portal_agent, 
            workflow_agent
        ], 
        port=app_port
    )