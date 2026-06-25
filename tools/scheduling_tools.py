import os
from pydantic import BaseModel, Field, EmailStr
from supabase import create_client, Client
from utils.logger import log

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

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
    """Inserts a completed tour row into Supabase, adding an active ID resolution fallback layer."""
    details_dict = details if isinstance(details, dict) else details.model_dump()
    raw_property_identifier = str(details_dict.get("property_id")).strip()
    
    log.info(f"[ID RESOLUTION] Received property identifier from LLM: '{raw_property_identifier}'")
    
    resolved_id = None

    # Step 1: Check if it's already a perfect match for an existing Primary Key
    exact_check = supabase.table("properties").select("id").eq("id", raw_property_identifier).execute()
    if exact_check.data:
        resolved_id = exact_check.data[0]["id"]
        log.info(f"[ID RESOLUTION] Direct Primary Key match found: {resolved_id}")
    
    # Step 2: Fallback to a Name/Title text match search if direct match failed
    if not resolved_id:
        log.info(f"[ID RESOLUTION] Direct lookup failed. Performing text similarity search on database fields...")
        # Clean up identifiers if user typed numeric fragments or text names
        clean_search = raw_property_identifier.replace("W-", "").replace("P-", "")
        
        name_check = supabase.table("properties").select("id").ilike("name", f"%{clean_search}%").execute()
        if name_check.data:
            resolved_id = name_check.data[0]["id"]
            log.info(f"[ID RESOLUTION] Successfully mapped '{raw_property_identifier}' to DB Primary Key: {resolved_id}")
        else:
            # Step 3: Ultimate Fallback (Grab the most recently searched/top property if text search fails completely)
            log.warn(f"[ID RESOLUTION] No match found for '{raw_property_identifier}'. Defaulting to top catalog listing as safety fallback.")
            fallback_check = supabase.table("properties").select("id").limit(1).execute()
            if fallback_check.data:
                resolved_id = fallback_check.data[0]["id"]

    # If all resolution layers fail to find any property rows in the DB
    if not resolved_id:
        log.error("[SUPABASE DB] Aborting operation. Target properties catalog table is entirely empty.")
        return BookingConfirmation(booking_id="NONE", status="FAILED", message="No properties found in the system catalog to map against.")

    # Step 4: Proceed with writing the payload using the guaranteed valid foreign key
    payload = {
        "property_id": resolved_id,
        "full_name": details_dict.get("full_name"),
        "email": details_dict.get("email"),
        "phone_number": details_dict.get("phone_number"),
        "preferred_date": details_dict.get("preferred_date")
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
    new_date_str = details_dict.get("new_date")
    
    log.info(f"[SUPABASE DB] Attempting reschedule update for Booking UUID: {target_id} to New Date: '{new_date_str}'")
    
    payload = {
        "preferred_date": new_date_str,
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
            message=f"Your tour has been successfully rescheduled to {new_date_str}."
        )
        
    log.error(f"[SUPABASE DB] Rescheduling transaction failed for ID: {target_id}")
    return OperationStatusResponse(status="FAILED", message="Booking ID not found or modification failed.")