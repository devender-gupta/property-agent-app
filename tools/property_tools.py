import os
from typing import List, Optional
from pydantic import BaseModel, Field
from supabase import create_client, Client
from utils.logger import log

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY or SUPABASE_SERVICE_ROLE_KEY must be set.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

class PropertySearchInput(BaseModel):
    max_price: Optional[int] = Field(None, description="Maximum budget/monthly rent price.")
    beds: Optional[int] = Field(None, description="Minimum number of bedrooms requested.")
    style: Optional[str] = Field(None, description="Architectural or interior style requested.")
    amenities: Optional[List[str]] = Field(None, description="List of specific amenities required.")

class PropertyResponse(BaseModel):
    id: str
    name: str
    beds: int
    price: int
    style: str
    amenities: List[str]
    description: str

def search_properties_api(filters: PropertySearchInput) -> List[PropertyResponse]:
    """Queries the live Supabase property catalog based on active filters."""
    filter_dict = filters if isinstance(filters, dict) else filters.model_dump(exclude_none=True)
    log.info("Property search started filters=%s", filter_dict)
    
    query = supabase.table("properties").select("*")
    
    max_price = filter_dict.get("max_price")
    beds = filter_dict.get("beds")
    style = filter_dict.get("style")
    amenities = filter_dict.get("amenities")
    
    if max_price:
        query = query.lte("price", max_price)
    if beds is not None:
        query = query.gte("beds", beds)
    if style:
        query = query.ilike("style", style)

    # Seed data stores title-cased amenities; normalize incoming values to match.
    if amenities:
        normalized_amenities = [a.title() for a in amenities]
        query = query.contains("amenities", normalized_amenities)

    response = query.execute()
    log.info("Property search completed matches=%d", len(response.data))
    return [PropertyResponse(**item) for item in response.data]