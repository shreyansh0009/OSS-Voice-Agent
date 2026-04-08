"""
Salesforce Contact tools.

Tools:
  sf_create_contact   - Create a Contact (optionally linked to an Account)
  sf_get_contact      - Retrieve a Contact by ID
  sf_update_contact   - Update fields on a Contact
  sf_search_contacts  - Search contacts by name, email, or phone
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from providers.salesforce.client import SalesforceClient


async def create_contact(client: "SalesforceClient", args: dict) -> dict:
    data = {
        "FirstName": args.get("first_name", ""),
        "LastName": args["last_name"],
        "Email": args.get("email", ""),
        "Phone": args.get("phone", ""),
        "MobilePhone": args.get("mobile_phone", ""),
        "AccountId": args.get("account_id", ""),
        "Title": args.get("title", ""),
        "Description": args.get("description", ""),
    }
    data = {k: v for k, v in data.items() if v}
    return await client.create("Contact", data)


async def get_contact(client: "SalesforceClient", args: dict) -> dict:
    fields = args.get("fields") or [
        "Id", "FirstName", "LastName", "Email", "Phone", "MobilePhone", "AccountId", "Title"
    ]
    return await client.get("Contact", args["contact_id"], fields=fields)


async def update_contact(client: "SalesforceClient", args: dict) -> dict:
    contact_id = args.pop("contact_id")
    return await client.update("Contact", contact_id, args)


async def search_contacts(client: "SalesforceClient", args: dict) -> dict:
    term = args["search_term"].replace("'", "\\'")
    field = args.get("field", "Name")
    soql = (
        f"SELECT Id, FirstName, LastName, Email, Phone, AccountId "
        f"FROM Contact WHERE {field} LIKE '%{term}%' LIMIT {args.get('limit', 10)}"
    )
    return await client.query(soql)


TOOLS = [
    {
        "name": "sf_create_contact",
        "description": "Create a new Contact in Salesforce, optionally linked to an Account.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "last_name": {"type": "string", "description": "Last name (required)"},
                "first_name": {"type": "string"},
                "email": {"type": "string"},
                "phone": {"type": "string"},
                "mobile_phone": {"type": "string"},
                "account_id": {"type": "string", "description": "Salesforce Account ID to link"},
                "title": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["last_name"],
        },
        "handler": create_contact,
    },
    {
        "name": "sf_get_contact",
        "description": "Retrieve a Salesforce Contact record by ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "string"},
                "fields": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["contact_id"],
        },
        "handler": get_contact,
    },
    {
        "name": "sf_update_contact",
        "description": "Update fields on an existing Contact.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "string"},
                "Email": {"type": "string"},
                "Phone": {"type": "string"},
                "Title": {"type": "string"},
                "Description": {"type": "string"},
            },
            "required": ["contact_id"],
        },
        "handler": update_contact,
    },
    {
        "name": "sf_search_contacts",
        "description": "Search Contacts by name, email, or phone.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "search_term": {"type": "string"},
                "field": {
                    "type": "string",
                    "enum": ["Name", "Email", "Phone"],
                },
                "limit": {"type": "integer"},
            },
            "required": ["search_term"],
        },
        "handler": search_contacts,
    },
]
