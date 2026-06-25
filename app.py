import os
import asyncio
import webbrowser
from dotenv import load_dotenv

load_dotenv()

from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from agent_framework.devui import serve

# Import our updated tool list
from tools.property_tools import search_properties_api
from tools.scheduling_tools import (
    confirm_tour_booking, 
    list_customer_bookings_api,
    cancel_tour_booking_api,
    reschedule_tour_booking_api
)
from utils.logger import log

async def main():
    log.info("[SYSTEM INITIALIZATION] Spawning Microsoft Agent Framework Runtime...")
    
    chat_client = OpenAIChatClient(
        api_key=os.environ.get("OPENAI_API_KEY"),
        model=os.environ.get("AGENT_MODEL", "gpt-4o-mini")
    )

    # --- AGENT 1: PROPERTY DISCOVERY ---
    log.info("[CONFIG LOADING] Instantiating PropertyFinderAgent...")
    property_agent = Agent(
        name="PropertyFinderAgent",
        client=chat_client,
        tools=[search_properties_api],
        instructions="""
        You are an elite Real Estate AI Concierge. Help users find their dream property.
        
        Guidelines:
        1. Use search_properties_api to filter options based on budget, beds, style, or amenities.
        2. If the user mentions booking, touring, scheduling, or viewing a property, state clearly that you are transferring them to our Scheduling Coordinator.
        3. If the user asks to see their existing bookings, cancel a booking, or change an appointment time, tell them you are transferring them to the Customer Portal.
        """
    )

    # --- AGENT 2: TRANSACTIONAL RESERVATIONS ---
    log.info("[CONFIG LOADING] Instantiating TourSchedulerAgent...")
    scheduler_agent = Agent(
        name="TourSchedulerAgent",
        client=chat_client,
        tools=[confirm_tour_booking],
        instructions="""
        You are a Scheduling Coordinator. Your unique goal is to capture form details for a NEW property viewing tour.
        
        You MUST collect exactly these 4 pieces of information:
        1. Full Name
        2. Email Address
        3. Phone Number
        4. Preferred Date for the tour
        
        Guidelines:
        - Scan the conversational history first to see if they already gave any details.
        - Ask for missing items one by one.
        - Once all 4 details are present, execute the `confirm_tour_booking` tool.
        - After a SUCCESS confirmation, thank the user and conclude.
        """
    )

    # --- NEW AGENT 3: THE CUSTOMER PORTAL ---
    log.info("[CONFIG LOADING] Instantiating CustomerPortalAgent...")
    portal_agent = Agent(
        name="CustomerPortalAgent",
        client=chat_client,
        tools=[list_customer_bookings_api, cancel_tour_booking_api, reschedule_tour_booking_api],
        instructions="""
        You are the Customer Portal Representative. Your primary goal is to help users review, cancel, or reschedule their existing tour appointments.
        
        Capabilities & Protocols:
        1. LIST BOOKINGS: If the user wants to see their reservations, ask for their email address first if you don't have it, then run `list_customer_bookings_api`.
        2. CANCEL TOUR: If they want to cancel an appointment, display their bookings first to ensure you have the correct Booking UUID (`id`). Pass that UUID to `cancel_tour_booking_api`.
        3. RESCHEDULE TOUR: If they want to change their appointment time, confirm the specific Booking UUID and their new requested time, then invoke `reschedule_tour_booking_api`.
        
        Guidelines:
        - Be professional, secure, and helpful. 
        - Always rely on explicit data IDs returned by your tools rather than guessing string keys.
        """
    )
    log.info("[DEVUI] Initializing DevUI trace stream telemetry...")
    
    # Pack your agent nodes into an array list so the UI can construct the map layout
    agent_cluster = [property_agent, scheduler_agent, portal_agent]
    
    # Launch the background UI server on local port 8000
    os.environ["DEVUI_AUTH_TOKEN"] = "devpass"
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, lambda: serve(entities=agent_cluster, port=8000, auto_open=True))
    
    log.info("[DEVUI] Local dashboard server actively streaming on http://localhost:8000")
    print("🌐 Launching DevUI Dashboard Panel in your default browser...")
    webbrowser.open("http://localhost:8000")

    log.info("[SYSTEM READY] 3-Agent Graph Compiled. Starting conversation run loop.")
    print("\n🤖 Property Assistant Online. How can I help you find a home today?")
    
    active_agent = property_agent
    
    # FIX: Use a basic list of strings that is universally supported
    conversation_history = []
    
    while True:
        user_input = input("\nYou: ")
        if user_input.lower() in ["exit", "quit"]:
            log.info("[SYSTEM TERMINATION] Run loop stopped by user interface action.")
            break
            
        log.info(f"[USER INPUT] Raw message received: '{user_input}'")
        
        # --- 3-WAY ROUTING MATRIX ---
        user_msg_lower = user_input.lower()
        
        # Route to Customer Portal Agent (Lookups, Edits, Deletions)
        if any(kw in user_msg_lower for kw in ["my booking", "list booking", "show my", "cancel", "reschedule", "change my time"]):
            if active_agent != portal_agent:
                log.warning("[HANDOFF DETECTED] Swapping active execution focus -> CustomerPortalAgent")
                active_agent = portal_agent
                
        # Route to Tour Scheduler Agent (Brand New Booking Form)
        elif any(kw in user_msg_lower for kw in ["book", "tour", "schedule a visit", "view it"]):
            if active_agent != scheduler_agent:
                log.warning("[HANDOFF DETECTED] Swapping active execution focus -> TourSchedulerAgent")
                active_agent = scheduler_agent
                
        # Route back to Finder Agent if they want to clear context and search again
        elif any(kw in user_msg_lower for kw in ["search again", "find another", "looking for"]):
            if active_agent != property_agent:
                log.warning("[HANDOFF DETECTED] Swapping active execution focus -> PropertyFinderAgent")
                active_agent = property_agent

        # Append input turn to thread history
        conversation_history.append(user_input)
        
        # Execute the targeted active micro-agent
        result = await active_agent.run(conversation_history)
        
        response_text = str(result)
        conversation_history.append(response_text)
        
        log.info(f"[AGENT RESPONSE] Computed successfully by operational node.")
        print(f"\nAssistant: {response_text}")

        # Post-execution safety verify hook for completed bookings
        if active_agent == scheduler_agent and "SUCCESS" in response_text:
            log.info("[ROUTING ENGINE] New booking transaction finalized. Routing state back to entry point.")
            active_agent = property_agent
            
if __name__ == "__main__":
    asyncio.run(main())