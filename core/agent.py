"""
Base agent abstraction. Every specialized agent extends BaseAgent.
 
Flow:
  user speech -> STT -> agent.handle(transcript, session) -> TTS -> audio out
  agent.handle() returns AgentResponse which may include a HandoffSignal.
 
Optional capabilities (set via constructor):
  rag          - a BaseRetriever; retrieves context before every LLM call
  mcp_registry - an MCPRegistry; allows LLM to call external HTTP tools
"""
from __future__ import annotations
 
import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator
 
if TYPE_CHECKING:
    from providers.rag.base import BaseRetriever
    from providers.mcp.registry import MCPRegistry
 
logger = logging.getLogger(__name__)
 
# Token format the LLM uses to invoke an MCP tool:  [MCP:tool_name:{"arg":"val"}]
_MCP_PATTERN = re.compile(r"\[MCP:(\w+):(\{.*?\})\]", re.DOTALL)
 
# Control-marker cleanup — strips before yielding sentences to TTS
_REPLY_CLEAN_RE = re.compile(
    r"\[(?:HANDOFF|END_CALL|LANG|NAME|MOBILE|MCP|TOOL):[^\]]*\]|\[END_CALL\]"
)
_HANDOFF_RE     = re.compile(r"\[HANDOFF:(\w+)\]")
# Sentence boundary: whitespace immediately after .  !  ?
_SENT_BOUNDARY_RE = re.compile(r'(?<=[.!?])\s')
 
_LANG_NAME = {
    "hi": "Hindi (हिंदी)",
    "bn": "Bengali (বাংলা)",
    "te": "Telugu (తెలుగు)",
    "mr": "Marathi (मराठी)",
    "ta": "Tamil (தமிழ்)",
    "gu": "Gujarati (ગુજરાતી)",
    "kn": "Kannada (ಕನ್ನಡ)",
    "pa": "Punjabi (ਪੰਜਾਬੀ)",
    "ml": "Malayalam (മലയാളം)",
    "or": "Odia (ଓଡ଼ିଆ)",
    "en": "English",
}
 
# ── Assistant prefill starters ───────────────────────────────────────────────
# First word(s) the model must continue from — forces correct script from
# the very first token. Chosen to be natural filler-like openers.
#
# For Hindi: stops "Acha" (Roman) because the model sees "अच्छा" already typed.
# Extended to all supported non-English languages (was Hindi-only before).
_LANG_PREFILL: dict[str, str] = {
    "hi": "जी,",
    "bn": "হ্যাঁ,",
    "te": "అవును,",
    "mr": "हो,",
    "ta": "ஆம்,",
    "gu": "હા,",
    "kn": "ಹೌದು,",
    "pa": "ਹਾਂ,",
    "ml": "ശരി,",
    "or": "ହଁ,",
}

# ── Token estimation ─────────────────────────────────────────────────────────
# Rough token budget for conversation history in LLM calls.
# 1 token ≈ 4 chars for English/Latin, ≈ 2 chars for Indian scripts.
_HISTORY_TOKEN_BUDGET = 1500  # ~1500 tokens for history, rest for system+RAG+user

def _estimate_tokens(text: str) -> int:
    """Fast token estimate: count ASCII vs non-ASCII to handle multilingual text."""
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    non_ascii   = len(text) - ascii_chars
    return (ascii_chars // 4) + (non_ascii // 2) + 1
 
 
@dataclass
class HandoffSignal:
    """Returned when an agent wants to transfer control to another agent."""
    target:  str
    message: str = ""
    data:    dict[str, Any] = field(default_factory=dict)
 
 
@dataclass
class AgentResponse:
    text:     str
    handoff:  HandoffSignal | None = None
    end_call: bool = False
 
 
class BaseAgent(ABC):
    name: str
    can_handoff_to: list[str] = []
 
    def __init__(
        self,
        llm,
        prompt:       str,
        rag:          "BaseRetriever | None" = None,
        mcp_registry: "MCPRegistry | None"   = None,
        rag_top_k:    int = 3,
    ):
        self.llm          = llm
        self.system_prompt = prompt
        self.rag           = rag
        self.mcp_registry  = mcp_registry
        self.rag_top_k     = rag_top_k
 
    @abstractmethod
    async def handle(self, transcript: str, session) -> AgentResponse:
        ...
 
    # ── RAG ──────────────────────────────────────────────────────────────────
 
    def _needs_rag(self, query: str) -> bool:
        """Skip RAG for very short or trivial utterances (saves ~300 tokens + latency)."""
        stripped = query.strip().lower()
        if len(stripped) < 8:
            return False
        trivial = {
            "yes", "no", "ok", "okay", "hello", "hi", "hey", "haan", "nahi",
            "ji", "ha", "hmm", "thanks", "thank you", "bye", "haa", "naa",
            "theek hai", "accha", "shukriya",
        }
        return stripped not in trivial

    async def _retrieve_context(self, query: str) -> str:
        if not self.rag:
            return ""
        if not self._needs_rag(query):
            logger.debug(f"[{self.name}] RAG skipped for trivial query: '{query[:30]}'")
            return ""
        chunks = await self.rag.retrieve(query, top_k=self.rag_top_k)
        if not chunks:
            return ""
        context = self.rag.format_context(chunks)
        logger.debug(f"[{self.name}] RAG retrieved {len(chunks)} chunks for query: {query[:60]}")
        return context
 
    # ── MCP ──────────────────────────────────────────────────────────────────
 
    async def _execute_mcp_calls(self, text: str, session) -> tuple[str, dict[str, Any]]:
        if not self.mcp_registry:
            return text, {}
 
        matches = _MCP_PATTERN.findall(text)
        if not matches:
            return text, {}
 
        results: dict[str, Any] = {}
        for tool_name, args_json in matches:
            try:
                args = json.loads(args_json)
            except json.JSONDecodeError:
                logger.warning(f"[{self.name}] Invalid MCP args JSON for tool '{tool_name}'")
                continue
 
            logger.info(f"[{self.name}] MCP call: {tool_name}({args})")
            result = await self.mcp_registry.call(tool_name, args)
 
            if result.success:
                results[tool_name] = result.data
                session.set(f"mcp_{tool_name}_result", result.data)
                logger.info(f"[{self.name}] MCP result: {tool_name} -> {str(result.data)[:100]}")
            else:
                logger.error(f"[{self.name}] MCP error: {tool_name} -> {result.error}")
                results[tool_name] = f"Error: {result.error}"
 
        return text, results
 
    # ── LLM helpers ──────────────────────────────────────────────────────────
 
    def _log_token_estimate(self, messages: list[dict]) -> None:
        """Log estimated token count for monitoring. Helps catch runaway usage."""
        total = sum(_estimate_tokens(m["content"]) for m in messages)
        logger.info(f"[{self.name}] LLM call ~{total} input tokens ({len(messages)} messages)")
        if total > 4000:
            logger.warning(f"[{self.name}] HIGH TOKEN COUNT: ~{total} tokens — check history/prompt size")

    def _build_messages(self, session, user_message: str, rag_context: str) -> list[dict]:
        """
        Build the full messages list (system + history + user) for an LLM call.
        Pure helper — no side effects, no I/O. Shared by _chat() and stream_handle().
        """
        system_content = self.system_prompt
 
        if rag_context:
            system_content += (
                "\n\n## Relevant Knowledge Base Context\n"
                "Use the following retrieved information to inform your response. "
                "Do not quote sources verbatim; use the information naturally.\n\n"
                + rag_context
            )
        if self.mcp_registry and self.mcp_registry.list_tools():
            system_content += "\n\n" + self.mcp_registry.tool_descriptions_for_prompt()
 
        lang_code        = session.get("language", "en")
        lang_instruction = session.get("language_instruction", "")
        lang_name        = _LANG_NAME.get(lang_code, "English")
 
        if lang_instruction:
            # Placed at the VERY END of system prompt so it's the last thing
            # the model reads before generating — models weight recency heavily.
            system_content += (
                f"\n\n"
                f"=====================================================\n"
                f"LANGUAGE LOCK — THIS OVERRIDES EVERYTHING ABOVE\n"
                f"=====================================================\n"
                f"CURRENT LANGUAGE: {lang_name}\n"
                f"{lang_instruction}\n"
                f"DO NOT write even one word in English or any other language.\n"
                f"If you cannot think of a word in {lang_name}, use the closest equivalent.\n"
                f"Start your response directly in {lang_name} — no preamble, no language switching.\n"
                f"====================================================="
            )
 
        clean_history = [
            msg for msg in session.history
            if not (
                msg["content"].startswith("[SYSTEM:") or
                msg["content"].startswith("[LANGUAGE INSTRUCTION]") or
                msg["content"].startswith("[LANG_SWITCH:") or
                msg["content"].startswith("[Switching to ") or
                msg["content"].startswith("[Understood. Switching") or
                msg["content"].startswith("[Understood. I will")
            )
        ]

        # Token-budgeted history: walk backwards, keep most recent messages
        # that fit within the budget. Prevents runaway input tokens.
        recent: list[dict] = []
        token_count = 0
        for msg in reversed(clean_history):
            msg_tokens = _estimate_tokens(msg["content"])
            if token_count + msg_tokens > _HISTORY_TOKEN_BUDGET and recent:
                break  # budget exhausted (always keep at least 1 message)
            recent.append(msg)
            token_count += msg_tokens
        recent.reverse()
 
        history_messages = []
        prev_lang = None
        for msg in recent:
            msg_lang = msg.get("lang", lang_code)
            if prev_lang is not None and msg_lang != prev_lang:
                switch_name = _LANG_NAME.get(msg_lang, msg_lang)
                history_messages.append({
                    "role":    "assistant",
                    "content": f"[Switching to {switch_name} as the user requested.]"
                })
            history_messages.append({"role": msg["role"], "content": msg["content"]})
            prev_lang = msg_lang
 
        messages = [{"role": "system", "content": system_content}]
        messages.extend(history_messages)
 
        # ── User message ──────────────────────────────────────────────────────
        messages.append({"role": "user", "content": user_message})
 
        # ── Language reminder — final anchor before assistant generates ───────
        # After a long history the system prompt instruction loses weight.
        # This re-anchors it as close as possible to the output token stream.
        # Only injected for non-English sessions.
        if lang_code != "en" and lang_instruction:
            messages.append({
                "role": "user",
                "content": (
                    f"[SYSTEM REMINDER — DO NOT SPEAK THIS ALOUD: "
                    f"Reply in {lang_name} only. "
                    f"Every word must be in the correct script. "
                    f"No Roman transliteration. No English. No mixing.]"
                )
            })
 
        # ── Assistant prefill — strongest language lock available ─────────────
        # Inject the START of the assistant reply in the target language.
        # The model must CONTINUE from this token — it physically cannot
        # open with a Roman/English word.
        #
        # PATCH 1: extended from Hindi-only to ALL non-English languages.
        # PATCH 2: prefill is now a real word, not "".
        #          An empty prefill does nothing. A real word forces script.
        if lang_code != "en" and lang_code in _LANG_PREFILL and lang_instruction:
            messages.append({
                "role":    "assistant",
                "content": _LANG_PREFILL[lang_code]
            })
 
        return messages
 
    def _preprocess_transcript(self, transcript: str, session) -> str:
        """
        Hook for subclasses to transform the transcript before it reaches the LLM.
        Default: no-op. Override (e.g. in HelloAgent) to inject structured data.
        """
        return transcript
 
    async def _chat(self, session, user_message: str) -> str:
        """
        Full pipeline:
          1. (Optional) Preprocess transcript (subclass hook)
          2. (Optional) Retrieve RAG context for the user message
          3. Build messages (system + history + user)
          4. Call LLM
          5. (Optional) Execute any MCP tool calls in the response
          6. If MCP tools were called, do a follow-up LLM turn with results
          7. Update session history and return final reply
        """
        user_message = self._preprocess_transcript(user_message, session)
        rag_context  = await self._retrieve_context(user_message)
        messages     = self._build_messages(session, user_message, rag_context)
        self._log_token_estimate(messages)

        reply = await self.llm.chat(messages)

        reply, mcp_results = await self._execute_mcp_calls(reply, session)

        if mcp_results:
            tool_summary = "\n".join(
                f"Result of {name}: {result}" for name, result in mcp_results.items()
            )
            # Compact follow-up: only system + last user + assistant + tool results
            # (avoids re-sending entire history, saves ~50% tokens on MCP turns)
            follow_up_messages = [
                messages[0],  # system prompt
                {"role": "user",      "content": user_message},
                {"role": "assistant", "content": reply},
                {"role": "user",      "content": f"[TOOL_RESULTS]\n{tool_summary}\n\nNow respond to the customer based on these results. Be concise."},
            ]
            reply = await self.llm.chat(follow_up_messages)
 
        session.add_message("user",      user_message)
        session.add_message("assistant", reply)
 
        return reply
 
    async def stream_handle(self, transcript: str, session):
        """
        Streaming alternative to handle().
 
        Yields (sentence, None) for each TTS-ready sentence as tokens arrive
        from the LLM, then yields (None, AgentResponse) once complete.
 
        Cuts time-to-first-audio by ~50-70%: TTS starts on the first sentence
        while the LLM is still generating the rest of the reply.
 
        MCP tool calls (if any) are processed after streaming finishes and their
        follow-up reply is yielded as sentences before the final AgentResponse.
        """
        transcript  = self._preprocess_transcript(transcript, session)
        rag_context = await self._retrieve_context(transcript)
        messages    = self._build_messages(session, transcript, rag_context)
        self._log_token_estimate(messages)

        # Stream tokens and emit complete sentences / clauses as they accumulate.
        # Primary split: sentence-ending punctuation (.!?)
        # Secondary split: commas after 40+ chars — gets TTS started sooner on
        # long sentences without producing awkwardly short fragments.
        full_reply = ""
        buf        = ""

        async for token in self.llm.stream_chat(messages):
            full_reply += token
            buf        += token

            while True:
                # Primary: split on sentence boundaries (.!? followed by space)
                m = _SENT_BOUNDARY_RE.search(buf)
                if m:
                    sentence = buf[:m.start()]
                    buf      = buf[m.end():]
                    clean    = _REPLY_CLEAN_RE.sub("", sentence).strip()
                    if clean:
                        yield clean, None
                    continue

                # Secondary: split long clauses at commas to reduce first-audio latency.
                # Only fires when buffer has grown without a sentence boundary.
                if len(buf) > 40:
                    comma_pos = buf.rfind(", ")
                    if comma_pos > 20:
                        clause = buf[:comma_pos + 1]  # include the comma
                        buf    = buf[comma_pos + 2:]  # skip ", "
                        clean  = _REPLY_CLEAN_RE.sub("", clause).strip()
                        if clean:
                            yield clean, None
                        continue

                break  # no split point found yet — wait for more tokens

        # Flush remaining buffer at end of stream
        if buf.strip():
            clean = _REPLY_CLEAN_RE.sub("", buf).strip()
            if clean:
                yield clean, None
 
        # Execute MCP tool calls on the completed reply
        full_reply, mcp_results = await self._execute_mcp_calls(full_reply, session)
 
        # Follow-up LLM call if MCP tools fired
        if mcp_results:
            tool_summary = "\n".join(
                f"Result of {name}: {result}" for name, result in mcp_results.items()
            )
            # Compact follow-up: system + last user + assistant + results only
            follow_up_messages = [
                messages[0],  # system prompt
                {"role": "user",      "content": transcript},
                {"role": "assistant", "content": full_reply},
                {"role": "user",      "content": f"[TOOL_RESULTS]\n{tool_summary}\n\nNow respond to the customer based on these results. Be concise."},
            ]
            full_reply   = await self.llm.chat(follow_up_messages)
            clean_follow = _REPLY_CLEAN_RE.sub("", full_reply).strip()
            for part in re.split(r'(?<=[.!?])\s+', clean_follow):
                part = part.strip()
                if part:
                    yield part, None
 
        # Update session history
        session.add_message("user",      transcript)
        session.add_message("assistant", full_reply)
 
        # Extract common data tags and save to session
        for tag in ("NAME", "MOBILE"):
            m = re.search(rf"\[{tag}:([^\]]+)\]", full_reply)
            if m:
                session.set(tag.lower(), m.group(1).strip())
 
        # Build and yield final AgentResponse
        hm      = _HANDOFF_RE.search(full_reply)
        handoff = None
        if hm and hm.group(1) in self.can_handoff_to:
            handoff = HandoffSignal(target=hm.group(1))
 
        end_call   = "[END_CALL]" in full_reply
        clean_text = _REPLY_CLEAN_RE.sub("", full_reply).strip()
        yield None, AgentResponse(text=clean_text, handoff=handoff, end_call=end_call)