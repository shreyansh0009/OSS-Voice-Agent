"""
HelloAgent: first contact — greets caller, gets name + mobile, hands off to screener.
"""
from __future__ import annotations

import re

from core.agent import BaseAgent, AgentResponse, HandoffSignal
from core.session import CallSession

# ── Mobile number extraction ──────────────────────────────────────────────────
_DIGIT_WORDS: dict[str, str] = {
    "zero": "0", "oh": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
}


def _parse_mobile(text: str) -> str | None:
    """
    Extract a 10-digit Indian mobile number from a transcript.

    Handles:
      - Digit form:  "9876543210"  or  "(947) 295-6565"
      - Word form:   "nine eight seven six five four three two one zero"
      - Country code: "91 9876543210" → strips leading 91
    Returns the 10-digit string or None if not found.
    """
    cleaned = re.sub(r"[\(\)\-\+\.]", " ", text)

    # Digit form
    digits = re.sub(r"\D", "", cleaned)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) == 10 and digits[0] in "6789":
        return digits
    if len(digits) > 10:
        candidate = digits[-10:]
        if candidate[0] in "6789":
            return candidate

    # Word form: "nine eight seven..."
    tokens = cleaned.lower().split()
    seq: list[str] = []
    for tok in tokens:
        if tok in _DIGIT_WORDS:
            seq.append(_DIGIT_WORDS[tok])
        else:
            if len(seq) >= 10:
                break
            seq = []

    if len(seq) >= 10:
        candidate = "".join(seq[:10])
        if candidate[0] in "6789":
            return candidate

    return None


class HelloAgent(BaseAgent):
    name = "hello"
    can_handoff_to = ["screener"]

    def _preprocess_transcript(self, transcript: str, session) -> str:
        """
        If the transcript contains a mobile number, extract it and inject it
        as a structured [PARSED_MOBILE:xxx] note so the LLM copies the exact
        digits instead of re-interpreting them (which causes hallucinated numbers).
        """
        mobile = _parse_mobile(transcript)
        if mobile:
            session.set("stated_mobile_raw", mobile)
            return (
                f"{transcript}\n"
                f"[PARSED_MOBILE:{mobile}]"
            )
        return transcript

    async def handle(self, transcript: str, session: CallSession) -> AgentResponse:
        reply = await self._chat(session, transcript)

        # ── Guard: LLM returned empty string ──────────────────────────────
        if not reply or not reply.strip():
            lang = session.get("language", "hi")
            fallback = (
                "क्षमा करें, क्या आप दोबारा बता सकते हैं?"
                if lang == "hi"
                else "I'm sorry, could you please repeat that?"
            )
            return AgentResponse(text=fallback)

        handoff     = self._parse_handoff(reply)
        end_call    = "[END_CALL]" in reply
        clean_reply = re.sub(
            r"\[(?:HANDOFF|END_CALL|LANG|NAME|MOBILE|PARSED_MOBILE|INTENT):[^\]]*\]|\[END_CALL\]",
            "",
            reply,
        ).strip()

        if handoff:
            name_match   = re.search(r"\[NAME:([^\]]+)\]", reply)
            mobile_match = re.search(r"\[MOBILE:([^\]]+)\]", reply)
            intent_match = re.search(r"\[INTENT:([^\]]+)\]", reply)
            if name_match:
                session.set("name", name_match.group(1).strip())
            if mobile_match:
                session.set("mobile", mobile_match.group(1).strip())
            if intent_match:
                session.set("intent", intent_match.group(1).strip())

            return AgentResponse(
                text=clean_reply,
                handoff=HandoffSignal(target=handoff),
            )

        if end_call:
            return AgentResponse(text=clean_reply, end_call=True)

        return AgentResponse(text=clean_reply)

    def _parse_handoff(self, text: str) -> str | None:
        match = re.search(r"\[HANDOFF:(\w+)\]", text)
        if match and match.group(1) in self.can_handoff_to:
            return match.group(1)
        return None
