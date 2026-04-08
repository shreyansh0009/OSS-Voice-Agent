"""
Generic SOQL / SOSL query tools — escape hatch for any Salesforce data need
not covered by the domain-specific tools.

Tools:
  sf_soql_query  - Run an arbitrary SELECT SOQL query
  sf_sosl_search - Run an arbitrary FIND SOSL search
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from providers.salesforce.client import SalesforceClient


async def soql_query(client: "SalesforceClient", args: dict) -> dict:
    return await client.query(args["soql"])


async def sosl_search(client: "SalesforceClient", args: dict) -> dict:
    return await client.search(args["sosl"])


TOOLS = [
    {
        "name": "sf_soql_query",
        "description": (
            "Run a raw SOQL SELECT query against Salesforce. "
            "Use when no specific tool covers your data need. "
            "Example: SELECT Id, Name FROM Account WHERE CreatedDate = TODAY"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "soql": {
                    "type": "string",
                    "description": "Full SOQL SELECT statement",
                },
            },
            "required": ["soql"],
        },
        "handler": soql_query,
    },
    {
        "name": "sf_sosl_search",
        "description": (
            "Run a SOSL FIND search across multiple Salesforce objects. "
            "Example: FIND {John Smith} IN NAME FIELDS RETURNING Lead(Id, Name), Contact(Id, Name)"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sosl": {
                    "type": "string",
                    "description": "Full SOSL FIND statement",
                },
            },
            "required": ["sosl"],
        },
        "handler": sosl_search,
    },
]
