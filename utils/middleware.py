from datetime import datetime

from agent_framework import FunctionMiddleware, FunctionInvocationContext

from tools.scheduling_tools import (
    BookingConfirmation,
    OperationStatusResponse,
    parse_and_normalize_date,
)
from utils.logger import log

# Function name -> (date field on the validated arguments model, failure-response factory)
_DATE_GUARDED_FUNCTIONS = {
    "confirm_tour_booking": ("preferred_date", lambda message: BookingConfirmation(
        booking_id="NONE", status="FAILED", message=message
    )),
    "reschedule_tour_booking_api": ("new_date", lambda message: OperationStatusResponse(
        status="FAILED", message=message
    )),
}


def _extract_date_field(arguments: object, date_field: str) -> object:
    """Find date_field in the validated arguments.

    For tools declared with a single Pydantic-model parameter (e.g.
    confirm_tour_booking(details: TourBookingInput)), FunctionInvocationContext.arguments
    arrives as {"details": {"preferred_date": ..., ...}} -- nested under the tool's own
    parameter name, not flattened. We check the top level first, then one level of nested
    dicts, so this keeps working regardless of the wrapping parameter's name.
    """
    if not isinstance(arguments, dict):
        return getattr(arguments, date_field, None)

    if date_field in arguments:
        return arguments[date_field]

    for value in arguments.values():
        if isinstance(value, dict) and date_field in value:
            return value[date_field]

    return None


class PastDateGuardMiddleware(FunctionMiddleware):
    """Rejects tour booking/reschedule calls whose requested date has already passed.

    The tools themselves only validate that a date string *parses*; this middleware
    is the single place that enforces it's also not in the past, so the check applies
    uniformly to both confirm_tour_booking and reschedule_tour_booking_api without
    duplicating logic in each tool.
    """

    async def process(self, context: FunctionInvocationContext, call_next) -> None:
        guard = _DATE_GUARDED_FUNCTIONS.get(context.function.name)
        if guard is None:
            await call_next()
            return

        date_field, build_failure_response = guard
        raw_date = _extract_date_field(context.arguments, date_field)

        date_success, normalized_or_error = parse_and_normalize_date(raw_date)
        if not date_success:
            await call_next()
            return

        parsed_dt = datetime.strptime(normalized_or_error, "%Y-%m-%d %H:%M:%S")
        if parsed_dt < datetime.now():
            log.warning(
                "Blocked %s: requested date %s is in the past",
                context.function.name,
                normalized_or_error,
            )
            # Set context.result and skip call_next() rather than raising
            # MiddlewareTermination -- termination ends the whole agent turn
            # immediately with the raw function result as the final output,
            # bypassing the follow-up model call that turns it into a reply.
            # Returning a normal result here lets the agent continue exactly
            # like a real FAILED tool response, so the model relays it to the user.
            context.result = build_failure_response(
                f"The requested date/time ({normalized_or_error}) is in the past. "
                "Please ask the user for a future date and time."
            )
            return

        await call_next()
