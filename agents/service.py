"""
ServiceAgent: handles complaints, service booking, warranty, and escalation.

MCP tools (from the DMS/scheduling API):
  get_service_slots(date, service_type) -> list of available times
  book_service_appointment(name, phone, date, time, service_type) -> confirmation

RAG: product knowledge base, service center info, warranty details.
"""
from __future__ import annotations

import re

from core.agent import BaseAgent, AgentResponse, HandoffSignal
from core.session import CallSession


class ServiceAgent(BaseAgent):
    name = "service"
    can_handoff_to = ["scheduler", "sales", "closer"]

    async def handle(self, transcript: str, session: CallSession) -> AgentResponse:
        reply = await self._chat(session, transcript)
        handoff  = self._parse_handoff(reply)
        end_call = "[END_CALL]" in reply

        # Capture address when LLM emits the tag
        address_match = re.search(r"\[ADDRESS:([^\]]+)\]", reply)
        if address_match:
            session.set("address", address_match.group(1).strip())

        clean_reply = re.sub(
            r"\[(?:HANDOFF|END_CALL|LANG|NAME|MOBILE|MCP|ADDRESS):[^\]]*\]|\[END_CALL\]",
            "", reply,
        ).strip()

        if handoff:
            return AgentResponse(
                text=clean_reply,
                handoff=HandoffSignal(
                    target=handoff,
                    data={
                        "customer_name":   session.get("customer_name", ""),
                        "customer_mobile": session.get("customer_mobile", ""),
                        "language":        session.get("language", ""),
                        "product":         session.get("product", ""),
                        "address":         session.get("address", ""),
                    },
                ),
            )
        if end_call:
            return AgentResponse(text=clean_reply, end_call=True)
        return AgentResponse(text=clean_reply)

    def _parse_handoff(self, text: str) -> str | None:
        match = re.search(r"\[HANDOFF:(\w+)\]", text)
        if match and match.group(1) in self.can_handoff_to:
            return match.group(1)
        return None
