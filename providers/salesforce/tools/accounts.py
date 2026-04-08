"""
Salesforce Account tools.

Tools:
  sf_get_account      - Retrieve an Account by ID
  sf_search_accounts  - Search accounts by name
  sf_create_account   - Create a new Account
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from providers.salesforce.client import SalesforceClient


async def get_account(client: "SalesforceClient", args: dict) -> dict:
    fields = args.get("fields") or [
        "Id", "Name", "Phone", "BillingCity", "BillingState", "Industry", "Type"
    ]
    return await client.get("Account", args["account_id"], fields=fields)


async def search_accounts(client: "SalesforceClient", args: dict) -> dict:
    term = args["name"].replace("'", "\\'")
    soql = (
        f"SELECT Id, Name, Phone, BillingCity, Industry, Type "
        f"FROM Account WHERE Name LIKE '%{term}%' LIMIT {args.get('limit', 10)}"
    )
    return await client.query(soql)


async def create_account(client: "SalesforceClient", args: dict) -> dict:
    data = {
        "Name": args["name"],
        "Phone": args.get("phone", ""),
        "Industry": args.get("industry", ""),
        "Type": args.get("type", ""),
        "BillingCity": args.get("billing_city", ""),
        "BillingState": args.get("billing_state", ""),
        "BillingCountry": args.get("billing_country", ""),
        "Description": args.get("description", ""),
    }
    data = {k: v for k, v in data.items() if v}
    return await client.create("Account", data)


TOOLS = [
    {
        "name": "sf_get_account",
        "description": "Retrieve a Salesforce Account record by ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string"},
                "fields": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["account_id"],
        },
        "handler": get_account,
    },
    {
        "name": "sf_search_accounts",
        "description": "Search Salesforce Accounts by name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Account name to search"},
                "limit": {"type": "integer"},
            },
            "required": ["name"],
        },
        "handler": search_accounts,
    },
    {
        "name": "sf_create_account",
        "description": "Create a new Account in Salesforce.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Account name (required)"},
                "phone": {"type": "string"},
                "industry": {"type": "string"},
                "type": {"type": "string", "description": "e.g. Customer, Partner, Prospect"},
                "billing_city": {"type": "string"},
                "billing_state": {"type": "string"},
                "billing_country": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["name"],
        },
        "handler": create_account,
    },
]
