"""
Tool registry — collects all Salesforce tools from each domain module.

To add a new domain:
  1. Create providers/salesforce/tools/my_domain.py
  2. Define a TOOLS list of ToolDef dicts (see existing modules)
  3. Import it here and append to _ALL_TOOL_LISTS
"""
from __future__ import annotations

from providers.salesforce.tools.leads import TOOLS as LEAD_TOOLS
from providers.salesforce.tools.contacts import TOOLS as CONTACT_TOOLS
from providers.salesforce.tools.opportunities import TOOLS as OPPORTUNITY_TOOLS
from providers.salesforce.tools.accounts import TOOLS as ACCOUNT_TOOLS
from providers.salesforce.tools.soql import TOOLS as SOQL_TOOLS

_ALL_TOOL_LISTS = [
    LEAD_TOOLS,
    CONTACT_TOOLS,
    OPPORTUNITY_TOOLS,
    ACCOUNT_TOOLS,
    SOQL_TOOLS,
]

# Flat dict: tool_name -> tool definition (includes "handler" key)
ALL_TOOLS: dict[str, dict] = {
    tool["name"]: tool
    for tool_list in _ALL_TOOL_LISTS
    for tool in tool_list
}


def list_tool_schemas() -> list[dict]:
    """Return tool definitions without the internal 'handler' key (for MCP tools/list)."""
    return [
        {k: v for k, v in tool.items() if k != "handler"}
        for tool in ALL_TOOLS.values()
    ]
