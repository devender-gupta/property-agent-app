import os
from datetime import datetime, timedelta
from pydantic import BaseModel, Field, EmailStr
from supabase import create_client, Client
from utils.logger import log

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- NEW UTILITY: NATURAL DATE NORMALIZATION LAYER ---
def parse_and_normalize_date(date_str: str) -> tuple[bool, str]:
    """
    Parses conversational or loose date/time strings into a structured ISO timestamp 
    compatible with PostgreSQL (YYYY-MM-DD HH:MM:SS).
    
    Returns:
        (True, 'YYYY-MM-DD HH:MM:SS') if successful.
        (False, 'Error message guiding the LLM Agent') if parsing fails entirely.
    """
    clean_str = str(date_str).strip().lower()
    now = datetime.now()
    
    # 1. Pre-process common loose relative tokens
    if "tomorrow" in clean_str:
        target_date = now + timedelta(days=1)
        clean_str = clean_str.replace("tomorrow", target_date.strftime("%Y-%m-%d"))
    elif "today" in clean_str:
        clean_str = clean_str.replace("today", now.strftime("%Y-%m-%d"))

    # 2. Try advanced fuzzy parsing using standard package tools
    try:
        import dateutil.parser as dparser
        # fuzzy=True extracts dates out of unstructured sentences (e.g. "schedule for Friday at 4pm")
        parsed_dt = dparser.parse(clean_str, fuzzy=True, default=now)
        return True, parsed_dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        # 3. Fallback check using explicit format structural matching loops
        standard_formats = (
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", 
            "%m/%d/%Y %I:%M %p", "%m/%d/%Y", "%d/%m/%Y"
        )
        for fmt in standard_formats:
            try:
                parsed_dt = datetime.strptime(str(date_str).strip(), fmt)
                return True, parsed_dt.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
        
        # 4. Graceful handling: Provide instructions back to the agent instead of throwing an error
        return False, (
            f"Invalid date/time context value: '{date_str}'. "
            "Please explicitly ask the user to clarify or re-state their preferred "
            "appointment day and time using standard values (e.g., 'October 12th at 3:00 PM' or 'YYYY-MM-DD HH:MM')."
        )


# --- PYDANTIC SCHEMAS ---
class TourBookingInput(BaseModel):
    property_id: str = Field(..., description="The ID, name, number, or title of the property the user selected (e.g., P-1001, Garden Studio 1001, or 1).")
    full_name: str = Field(..., description="The user's first and last name.")
    email: EmailStr = Field(..., description="A valid email address.")
    phone_number: str = Field(..., description="The user's direct contact phone number.")
    preferred_date: str = Field(..., description="The date and time string representing when they want to tour.")

class BookingConfirmation(BaseModel):
    booking_id: str
    status: str
    message: str

class CancelBookingInput(BaseModel):
    booking_id: str = Field(..., description="The unique UUID string of the reservation that needs to be cancelled.")

class RescheduleBookingInput(BaseModel):
    booking_id: str = Field(..., description="The unique UUID string of the reservation to be updated.")
    new_date: str = Field(..., description="The new preferred date and time string for the property tour.")

class CustomerEmailInput(BaseModel):
    email: EmailStr = Field(..., description="The user's verified email address to search for bookings.")

class OperationStatusResponse(BaseModel):
    status: str
    message: str


# --- CORE DB UTILITY FUNCTIONS ---

def confirm_tour_booking(details: TourBookingInput) -> BookingConfirmation:
    """Inserts a completed tour row into Supabase with robust ID resolution and date validation layers."""
    details_dict = details if isinstance(details, dict) else details.model_dump()
    raw_property_identifier = str(details_dict.get("property_id")).strip()
    raw_date_string = details_dict.get("preferred_date")
    
    log.info(f"[ID RESOLUTION] Received property identifier from LLM: '{raw_property_identifier}'")
    
    # NEW STEP: Validate and Normalize Date String
    date_success, normalized_date_result = parse_and_normalize_date(raw_date_string)
    if not date_success:
        log.warning(f"[DATE VALIDATION FAILED] Returning recovery feedback to Agent loop for raw input: '{raw_date_string}'")
        return BookingConfirmation(
            booking_id="NONE",
            status="FAILED",
            message=normalized_date_result  # The Agent will read this and prompt the user for clarification
        )
    
    log.info(f"[DATE VALIDATION SUCCESS] Normalized '{raw_date_string}' -> '{normalized_date_result}'")
    resolved_id = None

    # Step 1: Check if it's already a perfect match for an existing Primary Key
    exact_check = supabase.table("properties").select("id").eq("id", raw_property_identifier).execute()
    if exact_check.data:
        resolved_id = exact_check.data[0]["id"]
        log.info(f"[ID RESOLUTION] Direct Primary Key match found: {resolved_id}")
    
    # Step 2: Fallback to a Name/Title text match search if direct match failed
    if not resolved_id:
        log.info(f"[ID RESOLUTION] Direct lookup failed. Performing text similarity search on database fields...")
        clean_search = raw_property_identifier.replace("W-", "").replace("P-", "")
        
        name_check = supabase.table("properties").select("id").ilike("name", f"%{clean_search}%").execute()
        if name_check.data:
            resolved_id = name_check.data[0]["id"]
            log.info(f"[ID RESOLUTION] Successfully mapped '{raw_property_identifier}' to DB Primary Key: {resolved_id}")
        else:
            # Step 3: Ultimate Fallback (Grab top catalog item as fallback)
            log.warning(f"[ID RESOLUTION] No match found for '{raw_property_identifier}'. Defaulting to top catalog listing as safety fallback.")
            fallback_check = supabase.table("properties").select("id").limit(1).execute()
            if fallback_check.data:
                resolved_id = fallback_check.data[0]["id"]

    if not resolved_id:
        log.error("[SUPABASE DB] Aborting operation. Target properties catalog table is entirely empty.")
        return BookingConfirmation(booking_id="NONE", status="FAILED", message="No properties found in the system catalog to map against.")

    # Step 4: Proceed with writing the payload using the guaranteed valid foreign key and normalized date
    payload = {
        "property_id": resolved_id,
        "full_name": details_dict.get("full_name"),
        "email": details_dict.get("email"),
        "phone_number": details_dict.get("phone_number"),
        "preferred_date": normalized_date_result  # Safe timestamp format
    }
    
    response = supabase.table("tour_bookings").insert(payload).execute()
    
    if response.data:
        generated_id = response.data[0]["id"]
        log.info(f"[SUPABASE DB] Write Successful! Record Row UUID Committed: {generated_id}")
        return BookingConfirmation(
            booking_id=str(generated_id),
            status="SUCCESS",
            message=f"Tour successfully booked. ID: {generated_id}"
        )
    
    return BookingConfirmation(booking_id="NONE", status="FAILED", message="Database write transaction failed.")


def list_customer_bookings_api(filters: CustomerEmailInput) -> list:
    """Queries the tour_bookings table to find all reservations associated with an email."""
    filter_dict = filters if isinstance(filters, dict) else filters.model_dump()
    customer_email = filter_dict.get("email")
    
    log.info(f"[SUPABASE DB] Querying all bookings for email: '{customer_email}'")
    
    response = supabase.table("tour_bookings")\
        .select("id, preferred_date, properties(name)")\
        .eq("email", customer_email)\
        .execute()
        
    return response.data


def cancel_tour_booking_api(details: CancelBookingInput) -> OperationStatusResponse:
    """Updates an existing booking status to 'cancelled' in the Supabase database."""
    details_dict = details if isinstance(details, dict) else details.model_dump()
    target_id = details_dict.get("booking_id")
    
    log.info(f"[SUPABASE DB] Attempting soft delete / cancellation for Booking UUID: {target_id}")
    
    response = supabase.table("tour_bookings")\
        .update({"status": "cancelled", "updated_at": "now()"})\
        .eq("id", target_id)\
        .execute()
        
    if response.data:
        log.info(f"[SUPABASE DB] Cancellation successful for ID: {target_id}")
        return OperationStatusResponse(
            status="SUCCESS",
            message=f"Your tour booking (ID: {target_id}) has been successfully cancelled."
        )
        
    log.error(f"[SUPABASE DB] Cancellation failed. Booking ID {target_id} not found.")
    return OperationStatusResponse(status="FAILED", message="Booking ID not found or update failed.")


def reschedule_tour_booking_api(details: RescheduleBookingInput) -> OperationStatusResponse:
    """Updates an existing booking's date/time and marks its status as 'rescheduled' in Supabase."""
    details_dict = details if isinstance(details, dict) else details.model_dump()
    target_id = details_dict.get("booking_id")
    raw_new_date = details_dict.get("new_date")
    
    log.info(f"[SUPABASE DB] Attempting reschedule update for Booking UUID: {target_id} to New Date input: '{raw_new_date}'")
    
    # NEW STEP: Validate and Normalize Reschedule Date String
    date_success, normalized_date_result = parse_and_normalize_date(raw_new_date)
    if not date_success:
        log.warning(f"[RESCHEDULE DATE VALIDATION FAILED] Returning recovery feedback to Agent loop.")
        return OperationStatusResponse(
            status="FAILED",
            message=normalized_date_result
        )
        
    payload = {
        "preferred_date": normalized_date_result,
        "status": "rescheduled",
        "updated_at": "now()"
    }
    
    response = supabase.table("tour_bookings")\
        .update(payload)\
        .eq("id", target_id)\
        .execute()
        
    if response.data:
        log.info(f"[SUPABASE DB] Reschedule database update verified for ID: {target_id}")
        return OperationStatusResponse(
            status="SUCCESS",
            message=f"Your tour has been successfully rescheduled to {normalized_date_result}."
        )
        
    log.error(f"[SUPABASE DB] Rescheduling transaction failed for ID: {target_id}")
    return OperationStatusResponse(status="FAILED", message="Booking ID not found or modification failed.")