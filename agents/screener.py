"""
ScreenerAgent: discovers the caller's requirement and routes to the right specialist.

Routes to:
  service   — complaint, repair, installation, warranty
  sales     — product inquiry, pricing, availability
  scheduler — appointment booking, reschedule
  closer    — ticket tracking, call closure
"""
from __future__ import annotations

import re

from core.agent import BaseAgent, AgentResponse, HandoffSignal
from core.session import CallSession


class ScreenerAgent(BaseAgent):
    name = "screener"
    can_handoff_to = ["service", "sales", "scheduler", "closer"]

    async def handle(self, transcript: str, session: CallSession) -> AgentResponse:
        customer_name   = session.get("name", "")
        customer_mobile = session.get("mobile", "")
        customer_intent = session.get("intent", "")
        language        = session.get("language", session.current_language)

        # Force language lock
        if language:
            session.set_language(language)
            # Refresh language_instruction in session metadata so _build_messages
            # injects the correct LANGUAGE LOCK — set_language() only updates the
            # tracker, not the metadata key that _build_messages reads.
            from core.streaming_pipeline import _LANG_INSTRUCTIONS
            session.set("language_instruction", _LANG_INSTRUCTIONS.get(language, ""))

        context_prefix = self._build_context(customer_name, customer_mobile, language, customer_intent)
        enriched_transcript = f"{context_prefix}\nCustomer said: {transcript}"

        reply = await self._chat(session, enriched_transcript)

        handoff  = self._parse_handoff(reply)
        end_call = "[END_CALL]" in reply

        # Strip ALL control tags including [LANG:x]
        clean_reply = re.sub(
            r"\[HANDOFF:[^\]]+\]|\[END_CALL\]|\[LANG:[^\]]+\]",
            "", reply
        ).strip()

        if handoff:
            # Silent handoff — suppress any LLM-generated transition phrase
            # ("transferring you to service team", "let me connect you", etc.).
            # The destination agent immediately greets the customer; no bridge text needed.
            return AgentResponse(
                text="",
                handoff=HandoffSignal(target=handoff),
            )
        if end_call:
            return AgentResponse(text=clean_reply, end_call=True)
        return AgentResponse(text=clean_reply)

    async def stream_handle(self, transcript: str, session: CallSession):
        """
        Override the base streaming path.

        Screener responses are always one sentence, so streaming buys nothing.
        More importantly, base stream_handle() yields TTS sentences as they
        arrive from the LLM — the LLM often inserts a transition phrase
        ("I'll transfer you…") before [HANDOFF:xxx], which is already spoken
        by the time the handoff tag is detected.

        By going through handle() first (full reply in hand), we can suppress
        that phrase (handle() forces text="" on any handoff) before any audio
        is queued.
        """
        resp = await self.handle(transcript, session)
        clean_text = (resp.text or "").strip()
        if clean_text:
            for part in re.split(r'(?<=[.!?])\s+', clean_text):
                part = part.strip()
                if part:
                    yield part, None
        yield None, resp

    def _build_context(self, name: str, mobile: str, language: str, intent: str = "") -> str:
        lang_instruction = (
            "Reply in Hindi only. Tag every reply [LANG:hi]."
            if language == "hi"
            else "Reply in English only. Tag every reply [LANG:en]."
        )
        lines = [
            "[CONTEXT — do NOT ask again]",
            f"Customer name: {name}" if name else "",
            f"Customer mobile: {mobile}" if mobile else "",
            f"Language locked: {language}",
            lang_instruction,
        ]
        if intent:
            lines.append(
                f"Customer already stated their issue: \"{intent}\" — "
                f"DO NOT ask 'How can I help you?' again. Route immediately based on this."
            )
        lines.append("[END CONTEXT]")
        return "\n".join(l for l in lines if l)

    def _parse_handoff(self, text: str) -> str | None:
        match = re.search(r"\[HANDOFF:(\w+)\]", text)
        if match and match.group(1) in self.can_handoff_to:
            return match.group(1)
        return None
