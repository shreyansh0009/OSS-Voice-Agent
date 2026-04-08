"""
CallSession: tracks state for a single phone call.

Persists:
  - which agent is currently active
  - full conversation history (for LLM context)
  - arbitrary metadata that agents can read/write (passed across handoffs)
  - detected language (for multilingual support)

Fix vs previous version:
  - switch_agent() now accepts an explicit lang_code so the receiving agent
    inherits the correct locked language from the handoff data, not whatever
    the tracker last detected. Without this, a false-English detection during
    the Deepgram swap window would survive into the next agent.
  - set_language() now also resets the english_streak counter so a guarded
    English back-switch doesn't bleed across agent boundaries.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from providers.language.detector import LanguageTracker


@dataclass
class CallSession:
    session_id:    str             = field(default_factory=lambda: str(uuid.uuid4()))
    current_agent: str             = ""
    history:       list[dict]      = field(default_factory=list)
    metadata:      dict[str, Any]  = field(default_factory=dict)
    language_tracker: LanguageTracker = field(default_factory=LanguageTracker)

    @property
    def current_language(self) -> str:
        """The currently detected language code (e.g. 'hi', 'en', 'ta')."""
        return self.language_tracker.current_language

    def update_language(self, text: str) -> tuple[str, bool]:
        """
        Feed user transcript to the language tracker.
        Returns (language_code, switched) — switched=True when language changed.
        """
        return self.language_tracker.update(text)

    def set_language(self, lang_code: str) -> None:
        """
        Force-set the session language (e.g. from a handoff signal).
        Also resets the English-streak counter so a guarded back-switch
        from a previous turn doesn't carry over.
        """
        self.language_tracker.current_language = lang_code
        self.language_tracker._candidate       = lang_code
        self.language_tracker._candidate_count = 0
        self.language_tracker._english_streak  = 0   # FIX: reset streak on force-set

    def add_message(self, role: str, content: str) -> None:
        # Tag with current language so _chat can detect when language changed
        self.history.append({
            "role":    role,
            "content": content,
            "lang":    self.current_language,
        })

    def switch_agent(
        self,
        agent_name:    str,
        carry_history: bool = False,
        lang_code:     str | None = None,   # FIX: accept explicit language lock
    ) -> None:
        """
        Switch to a different agent.

        carry_history=True keeps the full conversation history (so the new
        agent has context). False resets history so the new agent starts
        fresh with only its own system prompt.

        lang_code: if provided, force-locks the language for the new agent.
        This ensures the language detected by HelloAgent survives handoffs
        even if a Deepgram swap artifact briefly flipped the tracker to 'en'.
        """
        self.current_agent = agent_name
        if not carry_history:
            self.history = []
        if lang_code:
            self.set_language(lang_code)   # FIX: lock language on handoff

    def set(self, key: str, value: Any) -> None:
        self.metadata[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.metadata.get(key, default)
