"""
CloserAgent: wraps up the call.

Confirms service/complaint booking, collects feedback,
and ends the call warmly.
"""
from __future__ import annotations

import re

from core.agent import BaseAgent, AgentResponse, HandoffSignal
from core.session import CallSession


class CloserAgent(BaseAgent):
    name = "closer"
    can_handoff_to = ["screener"]

    async def handle(self, transcript: str, session: CallSession) -> AgentResponse:
        language = session.get("language", session.current_language)
        print("versha debug language:", session.get("language"), session.current_language)
        reply = await self._chat(session, transcript)
        print("versha debugtranscript:", transcript)
        print("versha reply:", reply)
        has_end_call = "[END_CALL]" in reply
        print("versha debug has_end_call:", has_end_call)
        handoff      = self._parse_handoff(reply)

        # Strip ALL control tags including [LANG:x]
        clean_reply = re.sub(
            r"\[HANDOFF:[^\]]+\]|\[END_CALL\]|\[LANG:[^\]]+\]",
            "", reply
        ).strip()
        print("versha debug clean_reply:", clean_reply)
        

        # Detect if caller is done
        caller_done = self._caller_is_done(transcript)

        if caller_done or has_end_call:
            goodbye = (
                "Thank you for calling Godrej Customer Care! "
                "We're always here to help — have a wonderful day!"
            )
            return AgentResponse(text=goodbye, end_call=True)

        clean_reply = re.sub(r"\[(?:HANDOFF|END_CALL|LANG|NAME|MOBILE):[^\]]*\]|\[END_CALL\]", "", reply).strip()
        return AgentResponse(text=clean_reply, end_call=False)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _contains_goodbye_phrase(self, text: str) -> bool:
        """Phrases that only appear when the call is genuinely wrapping up."""
        lower = text.lower()
        return any(p in lower for p in [
            "have a fantastic day", "have a great day", "have a wonderful day",
            "goodbye", "take care", "drive safe",
            "looking forward to working with you",
            "calling godrej", "choosing godrej",
            "शुभ दिन", "धन्यवाद",
        ])

    def _contains_offer_help(self, text: str) -> bool:
        """Detect "anything else I can help" — means we are still mid-conversation."""
        lower = text.lower()
        return any(p in lower for p in [
            "anything else i can help",
            "anything else i can assist",
            "is there anything else",
            "anything else you",
        ])

    def _strip_after_offer_help(self, text: str) -> str:
        """Keep everything up to and including the "anything else?" question."""
        patterns = [
            "anything else i can help",
            "anything else i can assist",
            "is there anything else",
            "anything else you",
        ]
        lower = text.lower()
        cut = len(text)
        for p in patterns:
            idx = lower.find(p)
            if idx != -1:
                # Include the full sentence containing the pattern
                end = text.find("?", idx)
                if end != -1:
                    cut = min(cut, end + 1)
        return text[:cut].strip()

    def _caller_is_done(self, transcript: str) -> bool:
        lower = transcript.lower().strip()
        done_phrases = [
            "no", "nope", "nothing", "that's all", "thats all", "that's it",
            "thats it", "no thanks", "no thank you", "i'm good", "im good",
            "bye", "goodbye", "talk later", "have a good", "take care",
            "nahi", "bas", "bas itna hi", "theek hai", "dhanyavad", "shukriya",
            "thank you", "thanks", "ok bye", "okay bye",
        ]
        return any(
            lower == p or lower.startswith(p + " ") or lower.endswith(" " + p)
            for p in done_phrases
            
        )
