from providers.mcp.registry import MCPRegistry
from providers.rag.base import BaseRetriever
 
 
def __init__(
    self,
    llm,
    prompt: str,
    persona: str = "",           
    rag: "BaseRetriever | None" = None,
    mcp_registry: "MCPRegistry | None" = None,
    rag_top_k: int = 3,
):
    self.llm = llm
    self.system_prompt = (persona + "\n\n---\n\n" + prompt) if persona else prompt  # ← change this line
    self.rag = rag
    self.mcp_registry = mcp_registry
    self.rag_top_k = rag_top_k