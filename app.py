import os
import asyncio
from dotenv import load_dotenv

load_dotenv()

from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from agent_framework.orchestrations import HandoffBuilder
from agent_framework._types import Message

# Import our updated tool list
from tools.property_tools import search_properties_api
from tools.scheduling_tools import (
    confirm_tour_booking, 
    list_customer_bookings_api,
    cancel_tour_booking_api,
    reschedule_tour_booking_api
)
from utils.logger import log

# --- INITIALIZE THE SHARED COMPONENTS ---
chat_client = OpenAIChatClient(
    api_key=os.environ.get("OPENAI_API_KEY"),
    model=os.environ.get("AGENT_MODEL", "gpt-4o-mini")
)

# --- AGENT 1: PROPERTY DISCOVERY ---
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

# --- AGENT 2: TRANSACTIONAL RESERVATIONS ---
scheduler_agent = Agent(
    name="TourSchedulerAgent",
    client=chat_client,
    tools=[confirm_tour_booking],
    require_per_service_call_history_persistence=True,
    instructions="""
    You are a Scheduling Coordinator. Your unique goal is to capture form details for a NEW property viewing tour.
    
    You MUST collect exactly these 4 pieces of information:
    1. Full Name
    2. Email Address
    3. Phone Number
    4. Preferred Date and Time for the tour
    
    Guidelines:
    - Ask for missing items one by one.
    - Once all 4 details are present, execute the `confirm_tour_booking` tool.
    - After a SUCCESS confirmation, let the user know their tour is secured and that they can talk to the PropertyFinderAgent if they want to search for more homes.
    """
)

# --- AGENT 3: THE CUSTOMER PORTAL ---
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
    """
)

# --- BUILD THE EXPERT ORCHESTRATION GRAPH AT THE MODULE ROOT ---
log.info("[GRAPH BUILD] Compiling Handoff Builder Matrix...")
builder = HandoffBuilder(participants=[property_agent, scheduler_agent, portal_agent])
builder.with_start_agent(property_agent)  # Setting the initial starting node explicitly

# Fix parameter name back to 'description'
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
    description="Hand off back to the finder agent when the user wants to look for more properties."
)
builder.add_handoff(
    source=portal_agent, 
    targets=[property_agent], 
    description="Hand off back to the finder agent when they are done managing their existing appointments."
)

# This exposes the static entry point cleanly to the dynamic loading module parser
workflow = builder.build()

# --- STANDALONE TERMINAL EXECUTION LOOP ---
async def start_terminal_chat():
    print("\n🤖 Multi-Agent Property Assistant Online. How can I help you find a home today?")
    conversation_stream = []
    
    while True:
        try:
            user_input = input("\nYou: ")
            if user_input.lower() in ["exit", "quit"]:
                break
                
            conversation_stream.append(Message(role="user", content=user_input))
            result_events = await workflow.run(conversation_stream)
            
            response_text = "I am processing your request..."
            if isinstance(result_events, list):
                for event in reversed(result_events):
                    if hasattr(event, "type") and event.type == "request_info":
                        if hasattr(event, "data") and hasattr(event.data, "agent_response"):
                            response_text = str(event.data.agent_response)
                            break
                            
            print(f"\nAssistant: {response_text}")
            
        except Exception as e:
            print(f"\nAssistant: Sorry, I encountered an operational block. (Error: {str(e)})")

# Execute the local loop ONLY when running this file directly
if __name__ == "__main__":
    asyncio.run(start_terminal_chat())