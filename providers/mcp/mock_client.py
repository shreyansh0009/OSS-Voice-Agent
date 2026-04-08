"""
MockMCPRegistry — drop-in replacement for MCPRegistry while the real
Salesforce DMS MCP server is not yet connected.

Returns realistic fake responses for all Godrej DMS tools so the full
agent conversation flow (complaint → booking → confirmation) works end-to-end.

Usage (automatic via registry_godrej.py):
  Set MCP_MOCK=true in .env, or simply don't configure mcp_servers.json.
  When the real server is ready: add mcp_servers.json and remove MCP_MOCK.

Fake data behaviour:
  - book_service_appointment  → returns a fake SR number (SRN + 6-digit random)
  - register_complaint        → returns a fake complaint ID and SRN
  - get_service_slots         → returns 3 available time slots for tomorrow/day-after
  - check_warranty            → returns warranty status based on product category
  - get_inventory             → returns availability info for the product asked
"""
from __future__ import annotations

import logging
import random
import string
from datetime import date, timedelta

from providers.mcp.client import MCPToolResult

logger = logging.getLogger(__name__)


def _rand_id(prefix: str, length: int = 6) -> str:
    return prefix + "".join(random.choices(string.digits, k=length))


_APPT_WINDOW_START = 9.0    # 9:00 AM
_APPT_WINDOW_END   = 18.5   # 6:30 PM


def _parse_hour(slot: str) -> float | None:
    """
    Extract the start hour (as a float, 24-hour) from a slot string.
    Handles formats like: "10 AM", "6:30 PM", "14:00", "tomorrow 9 AM – 12 PM".
    Returns None if no time can be parsed.
    """
    import re
    # Try HH:MM 24-hour first (e.g. "14:00")
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", slot)
    if m:
        return int(m.group(1)) + int(m.group(2)) / 60

    # Try H[:MM] AM/PM
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(AM|PM)\b", slot, re.IGNORECASE)
    if m:
        hour = int(m.group(1))
        mins = int(m.group(2)) if m.group(2) else 0
        meridiem = m.group(3).upper()
        if meridiem == "PM" and hour != 12:
            hour += 12
        elif meridiem == "AM" and hour == 12:
            hour = 0
        return hour + mins / 60

    return None


def _mock_book_service_appointment(arguments: dict) -> MCPToolResult:
    slot = arguments.get("slot") or arguments.get("preferred_slot") or arguments.get("arg", "")

    if slot:
        hour = _parse_hour(slot)
        if hour is not None and not (_APPT_WINDOW_START <= hour < _APPT_WINDOW_END):
            return MCPToolResult(
                tool_name="book_service_appointment",
                success=False,
                error=(
                    "Requested time is outside the service window. "
                    "Godrej service appointments are available between 9:00 AM and 6:30 PM only. "
                    "Please choose a time within this window."
                ),
            )

    if not slot:
        slot = "tomorrow 10 AM"

    sr_number  = _rand_id("SRN", 8)
    csn        = _rand_id("CSN", 3)
    return MCPToolResult(
        tool_name="book_service_appointment",
        success=True,
        data=(
            f"Appointment confirmed. Service Request Number: {sr_number}. "
            f"CSN: {csn}. "
            f"A Godrej SmartBuddy engineer will visit on {slot}. "
            f"Engineer will contact you within 4 hours of the scheduled slot."
        ),
    )


def _mock_register_complaint(arguments: dict) -> MCPToolResult:
    srn        = _rand_id("SRN", 8)
    product    = arguments.get("product") or arguments.get("appliance") or "appliance"
    issue      = arguments.get("issue") or arguments.get("problem") or "reported issue"
    tomorrow   = (date.today() + timedelta(days=1)).strftime("%d %b %Y")
    return MCPToolResult(
        tool_name="register_complaint",
        success=True,
        data=(
            f"Complaint registered successfully. Service Request Number: {srn}. "
            f"Product: {product}. Issue: {issue}. "
            f"A Godrej engineer will visit by {tomorrow}. "
            f"You will receive a confirmation SMS on your registered mobile."
        ),
    )


def _mock_get_service_slots(arguments: dict) -> MCPToolResult:
    tomorrow   = (date.today() + timedelta(days=1)).strftime("%d %b %Y")
    day_after  = (date.today() + timedelta(days=2)).strftime("%d %b %Y")
    return MCPToolResult(
        tool_name="get_service_slots",
        success=True,
        data=(
            f"Available service slots: "
            f"(1) {tomorrow} 9 AM – 12 PM, "
            f"(2) {tomorrow} 2 PM – 5 PM, "
            f"(3) {day_after} 10 AM – 1 PM. "
            f"All slots are subject to engineer availability in your area."
        ),
    )


def _mock_check_warranty(arguments: dict) -> MCPToolResult:
    product = (
        arguments.get("product") or
        arguments.get("appliance") or
        arguments.get("model") or
        "your appliance"
    )
    product_lower = product.lower()
    if "ac" in product_lower or "air condition" in product_lower:
        coverage = "5-year comprehensive warranty (for purchases from Sept 2024 onwards) and 10-year compressor warranty"
    elif "fridge" in product_lower or "refrigerator" in product_lower:
        coverage = "1-year comprehensive warranty + 5-year compressor warranty"
    elif "washing" in product_lower or "washer" in product_lower:
        coverage = "2-year comprehensive warranty"
    elif "microwave" in product_lower:
        coverage = "1-year comprehensive warranty"
    elif "freezer" in product_lower:
        coverage = "5-year warranty"
    else:
        coverage = "1-year comprehensive warranty"
    return MCPToolResult(
        tool_name="check_warranty",
        success=True,
        data=(
            f"Warranty status for {product}: {coverage}. "
            f"Warranty is valid for products purchased from authorized Godrej dealers. "
            f"Please keep your purchase invoice for validation during service."
        ),
    )


def _mock_get_inventory(arguments: dict) -> MCPToolResult:
    product = (
        arguments.get("product") or
        arguments.get("model") or
        arguments.get("category") or
        "the requested product"
    )
    return MCPToolResult(
        tool_name="get_inventory",
        success=True,
        data=(
            f"{product} is currently available through authorized Godrej dealers "
            f"and on Amazon India / Flipkart (official Godrej storefront). "
            f"For exact pricing and stock, please visit www.godrejenterprises.com "
            f"or contact your nearest authorized dealer."
        ),
    )


_MOCK_HANDLERS = {
    "book_service_appointment": _mock_book_service_appointment,
    "register_complaint":       _mock_register_complaint,
    "get_service_slots":        _mock_get_service_slots,
    "check_warranty":           _mock_check_warranty,
    "get_inventory":            _mock_get_inventory,
}


class MockMCPRegistry:
    """
    Mimics the MCPRegistry interface but returns fake responses.
    No network calls are made.  Swap for the real MCPRegistry when
    the Salesforce DMS MCP server is connected.
    """

    def list_tools(self) -> list[str]:
        return list(_MOCK_HANDLERS.keys())

    async def call(self, tool_name: str, arguments: dict) -> MCPToolResult:
        handler = _MOCK_HANDLERS.get(tool_name)
        if not handler:
            return MCPToolResult(
                tool_name=tool_name,
                success=False,
                error=f"[Mock] No mock handler for tool '{tool_name}'",
            )
        logger.info(f"[Mock MCP] {tool_name}({arguments})")
        result = handler(arguments)
        logger.info(f"[Mock MCP] {tool_name} → {str(result.data)[:120]}")
        return result

    def tool_descriptions_for_prompt(self) -> str:
        lines = ["## Available MCP Tools\n"]
        descriptions = {
            "book_service_appointment": "Book a Godrej service engineer visit for a specific date/time slot",
            "register_complaint":       "Register a new complaint/service request for a Godrej appliance",
            "get_service_slots":        "Get available service appointment slots for the customer's area",
            "check_warranty":           "Check warranty status and coverage for a Godrej appliance",
            "get_inventory":            "Check product availability and where to purchase",
        }
        for name, desc in descriptions.items():
            lines.append(f"- `{name}`: {desc}")
        lines.append(
            '\nCall a tool by including `[MCP:tool_name:{"arg": "value"}]` in your response.'
        )
        return "\n".join(lines)
