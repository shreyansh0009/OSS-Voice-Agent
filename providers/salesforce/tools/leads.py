"""
Salesforce Lead tools.

Tools:
  sf_create_lead      - Create a new Lead record
  sf_get_lead         - Retrieve a Lead by ID
  sf_update_lead      - Update fields on a Lead
  sf_search_leads     - Search leads by name, email, or phone via SOQL
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from providers.salesforce.client import SalesforceClient


async def create_lead(client: "SalesforceClient", args: dict) -> dict:
    data = {
        "FirstName": args.get("first_name", ""),
        "LastName": args["last_name"],
        "Company": args.get("company", "Unknown"),
        "Email": args.get("email", ""),
        "Phone": args.get("phone", ""),
        "LeadSource": args.get("lead_source", "Phone"),
        "Description": args.get("description", ""),
    }
    # Drop empty strings to avoid Salesforce validation errors
    data = {k: v for k, v in data.items() if v}
    result = await client.create("Lead", data)
    return result


async def get_lead(client: "SalesforceClient", args: dict) -> dict:
    fields = args.get("fields") or ["Id", "FirstName", "LastName", "Email", "Phone", "Status", "Company"]
    return await client.get("Lead", args["lead_id"], fields=fields)


async def update_lead(client: "SalesforceClient", args: dict) -> dict:
    lead_id = args.pop("lead_id")
    return await client.update("Lead", lead_id, args)


async def search_leads(client: "SalesforceClient", args: dict) -> dict:
    term = args["search_term"].replace("'", "\\'")
    field = args.get("field", "Name")  # Name | Email | Phone
    soql = (
        f"SELECT Id, FirstName, LastName, Email, Phone, Status, Company "
        f"FROM Lead WHERE {field} LIKE '%{term}%' LIMIT {args.get('limit', 10)}"
    )
    return await client.query(soql)


TOOLS = [
    {
        "name": "sf_create_lead",
        "description": "Create a new Lead in Salesforce. Use when a caller expresses interest and you have their contact details.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "last_name": {"type": "string", "description": "Last name (required)"},
                "first_name": {"type": "string", "description": "First name"},
                "company": {"type": "string", "description": "Company name"},
                "email": {"type": "string", "description": "Email address"},
                "phone": {"type": "string", "description": "Phone number"},
                "lead_source": {"type": "string", "description": "e.g. Phone, Web, Referral"},
                "description": {"type": "string", "description": "Notes about the lead"},
            },
            "required": ["last_name"],
        },
        "handler": create_lead,
    },
    {
        "name": "sf_get_lead",
        "description": "Retrieve a Salesforce Lead record by its ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "lead_id": {"type": "string", "description": "Salesforce Lead record ID"},
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of fields to return",
                },
            },
            "required": ["lead_id"],
        },
        "handler": get_lead,
    },
    {
        "name": "sf_update_lead",
        "description": "Update one or more fields on an existing Lead.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "lead_id": {"type": "string", "description": "Salesforce Lead record ID"},
                "Status": {"type": "string", "description": "Lead status"},
                "Email": {"type": "string"},
                "Phone": {"type": "string"},
                "Description": {"type": "string"},
            },
            "required": ["lead_id"],
        },
        "handler": update_lead,
    },
    {
        "name": "sf_search_leads",
        "description": "Search Leads by name, email, or phone.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "search_term": {"type": "string", "description": "Text to search for"},
                "field": {
                    "type": "string",
                    "enum": ["Name", "Email", "Phone"],
                    "description": "Field to search in (default: Name)",
                },
                "limit": {"type": "integer", "description": "Max records (default 10)"},
            },
            "required": ["search_term"],
        },
        "handler": search_leads,
    },
]
