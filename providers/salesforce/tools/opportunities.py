"""
Salesforce Opportunity tools.

Tools:
  sf_create_opportunity  - Create a new Opportunity
  sf_get_opportunity     - Retrieve an Opportunity by ID
  sf_update_opportunity  - Update an Opportunity (stage, amount, close date, etc.)
  sf_search_opportunities - Search opportunities by name or account
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from providers.salesforce.client import SalesforceClient


async def create_opportunity(client: "SalesforceClient", args: dict) -> dict:
    data = {
        "Name": args["name"],
        "StageName": args.get("stage", "Prospecting"),
        "CloseDate": args["close_date"],  # YYYY-MM-DD
        "AccountId": args.get("account_id", ""),
        "Amount": args.get("amount"),
        "Description": args.get("description", ""),
        "LeadSource": args.get("lead_source", "Phone"),
    }
    data = {k: v for k, v in data.items() if v is not None and v != ""}
    return await client.create("Opportunity", data)


async def get_opportunity(client: "SalesforceClient", args: dict) -> dict:
    fields = args.get("fields") or [
        "Id", "Name", "StageName", "Amount", "CloseDate", "AccountId", "Probability"
    ]
    return await client.get("Opportunity", args["opportunity_id"], fields=fields)


async def update_opportunity(client: "SalesforceClient", args: dict) -> dict:
    opp_id = args.pop("opportunity_id")
    return await client.update("Opportunity", opp_id, args)


async def search_opportunities(client: "SalesforceClient", args: dict) -> dict:
    term = args["search_term"].replace("'", "\\'")
    soql = (
        f"SELECT Id, Name, StageName, Amount, CloseDate, AccountId "
        f"FROM Opportunity WHERE Name LIKE '%{term}%' "
        f"ORDER BY CloseDate DESC LIMIT {args.get('limit', 10)}"
    )
    return await client.query(soql)


TOOLS = [
    {
        "name": "sf_create_opportunity",
        "description": "Create a new Opportunity in Salesforce.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Opportunity name (required)"},
                "close_date": {"type": "string", "description": "Expected close date YYYY-MM-DD (required)"},
                "stage": {
                    "type": "string",
                    "description": "Stage: Prospecting, Qualification, Needs Analysis, Proposal, Closed Won, Closed Lost",
                },
                "account_id": {"type": "string", "description": "Linked Account ID"},
                "amount": {"type": "number", "description": "Deal value"},
                "lead_source": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["name", "close_date"],
        },
        "handler": create_opportunity,
    },
    {
        "name": "sf_get_opportunity",
        "description": "Retrieve a Salesforce Opportunity by ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "opportunity_id": {"type": "string"},
                "fields": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["opportunity_id"],
        },
        "handler": get_opportunity,
    },
    {
        "name": "sf_update_opportunity",
        "description": "Update an Opportunity (stage, amount, close date, etc.).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "opportunity_id": {"type": "string"},
                "StageName": {"type": "string"},
                "Amount": {"type": "number"},
                "CloseDate": {"type": "string"},
                "Description": {"type": "string"},
            },
            "required": ["opportunity_id"],
        },
        "handler": update_opportunity,
    },
    {
        "name": "sf_search_opportunities",
        "description": "Search Opportunities by name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "search_term": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["search_term"],
        },
        "handler": search_opportunities,
    },
]
