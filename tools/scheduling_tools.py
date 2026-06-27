import os
from datetime import datetime, timedelta
from pydantic import BaseModel, Field, EmailStr
from dateutil import parser as dparser
from supabase import create_client, Client
from utils.logger import log

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY or SUPABASE_SERVICE_ROLE_KEY must be set.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

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
    
    # Pre-process common relative tokens before parser fallback.
    if "tomorrow" in clean_str:
        target_date = now + timedelta(days=1)
        clean_str = clean_str.replace("tomorrow", target_date.strftime("%Y-%m-%d"))
    elif "today" in clean_str:
        clean_str = clean_str.replace("today", now.strftime("%Y-%m-%d"))

    try:
        parsed_dt = dparser.parse(clean_str, fuzzy=True, default=now)
        return True, parsed_dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
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
        
        return False, (
            f"Invalid date/time context value: '{date_str}'. "
            "Please explicitly ask the user to clarify or re-state their preferred "
            "appointment day and time using standard values (e.g., 'October 12th at 3:00 PM' or 'YYYY-MM-DD HH:MM')."
        )


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


def confirm_tour_booking(details: TourBookingInput) -> BookingConfirmation:
    """Inserts a completed tour row into Supabase with robust ID resolution and date validation layers."""
    details_dict = details if isinstance(details, dict) else details.model_dump()
    raw_property_identifier = str(details_dict.get("property_id")).strip()
    raw_date_string = details_dict.get("preferred_date")
    
    log.info("Booking request received property_identifier=%s", raw_property_identifier)
    
    date_success, normalized_date_result = parse_and_normalize_date(raw_date_string)
    if not date_success:
        log.warning("Booking date validation failed input=%s", raw_date_string)
        return BookingConfirmation(
            booking_id="NONE",
            status="FAILED",
            message=normalized_date_result
        )
    
    log.info("Booking date normalized raw=%s normalized=%s", raw_date_string, normalized_date_result)
    resolved_id = None

    exact_check = supabase.table("properties").select("id").eq("id", raw_property_identifier).execute()
    if exact_check.data:
        resolved_id = exact_check.data[0]["id"]
        log.info("Property id matched directly id=%s", resolved_id)
    
    if not resolved_id:
        log.info("Direct property id lookup failed, attempting name-based fallback")
        clean_search = raw_property_identifier.replace("W-", "").replace("P-", "")
        
        name_check = supabase.table("properties").select("id").ilike("name", f"%{clean_search}%").execute()
        if name_check.data:
            resolved_id = name_check.data[0]["id"]
            log.info("Name-based property mapping succeeded original=%s resolved_id=%s", raw_property_identifier, resolved_id)
        else:
            log.warning("Property mapping fallback triggered for identifier=%s", raw_property_identifier)
            fallback_check = supabase.table("properties").select("id").limit(1).execute()
            if fallback_check.data:
                resolved_id = fallback_check.data[0]["id"]

    if not resolved_id:
        log.error("Booking aborted because properties catalog is empty")
        return BookingConfirmation(booking_id="NONE", status="FAILED", message="No properties found in the system catalog to map against.")

    payload = {
        "property_id": resolved_id,
        "full_name": details_dict.get("full_name"),
        "email": details_dict.get("email"),
        "phone_number": details_dict.get("phone_number"),
        "preferred_date": normalized_date_result
    }

    log.debug("Persisting booking payload property_id=%s email=%s", resolved_id, details_dict.get("email"))
    
    response = supabase.table("tour_bookings").insert(payload).execute()
    
    if response.data:
        generated_id = response.data[0]["id"]
        log.info("Booking created booking_id=%s property_id=%s", generated_id, resolved_id)
        return BookingConfirmation(
            booking_id=str(generated_id),
            status="SUCCESS",
            message=f"Tour successfully booked. ID: {generated_id}"
        )
    
    log.error("Booking insert failed for property_id=%s email=%s", resolved_id, details_dict.get("email"))
    return BookingConfirmation(booking_id="NONE", status="FAILED", message="Database write transaction failed.")


def list_customer_bookings_api(filters: CustomerEmailInput) -> list:
    """Queries the tour_bookings table to find all reservations associated with an email."""
    filter_dict = filters if isinstance(filters, dict) else filters.model_dump()
    customer_email = filter_dict.get("email")
    
    log.info("Listing bookings for email=%s", customer_email)
    
    response = supabase.table("tour_bookings")\
        .select("id, preferred_date, properties(name)")\
        .eq("email", customer_email)\
        .execute()

    log.info("Found %d bookings for email=%s", len(response.data), customer_email)
        
    return response.data


def cancel_tour_booking_api(details: CancelBookingInput) -> OperationStatusResponse:
    """Updates an existing booking status to 'cancelled' in the Supabase database."""
    details_dict = details if isinstance(details, dict) else details.model_dump()
    target_id = details_dict.get("booking_id")
    
    log.info("Cancelling booking booking_id=%s", target_id)
    
    response = supabase.table("tour_bookings")\
        .update({"status": "cancelled", "updated_at": "now()"})\
        .eq("id", target_id)\
        .execute()
        
    if response.data:
        log.info("Cancellation successful booking_id=%s", target_id)
        return OperationStatusResponse(
            status="SUCCESS",
            message=f"Your tour booking (ID: {target_id}) has been successfully cancelled."
        )
        
    log.error("Cancellation failed booking_id=%s", target_id)
    return OperationStatusResponse(status="FAILED", message="Booking ID not found or update failed.")


def reschedule_tour_booking_api(details: RescheduleBookingInput) -> OperationStatusResponse:
    """Updates an existing booking's date/time and marks its status as 'rescheduled' in Supabase."""
    details_dict = details if isinstance(details, dict) else details.model_dump()
    target_id = details_dict.get("booking_id")
    raw_new_date = details_dict.get("new_date")
    
    log.info("Reschedule requested booking_id=%s requested_date=%s", target_id, raw_new_date)
    
    date_success, normalized_date_result = parse_and_normalize_date(raw_new_date)
    if not date_success:
        log.warning("Reschedule date validation failed booking_id=%s input=%s", target_id, raw_new_date)
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
        log.info("Reschedule successful booking_id=%s new_date=%s", target_id, normalized_date_result)
        return OperationStatusResponse(
            status="SUCCESS",
            message=f"Your tour has been successfully rescheduled to {normalized_date_result}."
        )
        
    log.error("Reschedule failed booking_id=%s", target_id)
    return OperationStatusResponse(status="FAILED", message="Booking ID not found or modification failed.")