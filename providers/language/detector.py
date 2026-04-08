"""
LanguageDetector — detects Indian languages from transcript text.

Supported languages:
    hi  — Hindi
    bn  — Bengali
    te  — Telugu
    mr  — Marathi
    ta  — Tamil
    gu  — Gujarati
    kn  — Kannada
    pa  — Punjabi
    ml  — Malayalam
    or  — Odia
    en  — English (default / fallback)

Switch policy (INTENT-ONLY)
---------------------------
Language only changes when the user EXPLICITLY requests it:

  1. Explicit request phrase:
       "let's talk in Hindi", "hindi mein baat karo",
       "please switch to English", "speak Tamil"
     → immediate switch, highest priority.

  2. Non-Latin Indian script in the utterance (Devanagari, Tamil, etc.):
     → immediate switch — completely unambiguous; if the user is typing/
       speaking in that script they obviously mean to use that language.

What does NOT trigger a switch:
  - Indian names in English ("my name is Priya")
  - Hinglish Roman-script words ("theek hai", "aur kya")
  - English words inside a Hindi conversation ("mera phone number hai 9876")
  - Mixed-language phrases ("I want service for my AC unit")

Rationale:
  Auto-switching on keyword/script fragments causes constant flip-flopping
  between languages — every Indian name flips to Hindi, every English word
  or acronym flips back to English.  The user must ask explicitly.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ── Unicode script → language ─────────────────────────────────────────────────
_SCRIPT_RANGES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"[\u0900-\u097F]"), "hi"),   # Devanagari (Hindi/Marathi)
    (re.compile(r"[\u0980-\u09FF]"), "bn"),   # Bengali
    (re.compile(r"[\u0C00-\u0C7F]"), "te"),   # Telugu
    (re.compile(r"[\u0B80-\u0BFF]"), "ta"),   # Tamil
    (re.compile(r"[\u0A80-\u0AFF]"), "gu"),   # Gujarati
    (re.compile(r"[\u0C80-\u0CFF]"), "kn"),   # Kannada
    (re.compile(r"[\u0A00-\u0A7F]"), "pa"),   # Gurmukhi (Punjabi)
    (re.compile(r"[\u0D00-\u0D7F]"), "ml"),   # Malayalam
    (re.compile(r"[\u0B00-\u0B7F]"), "or"),   # Odia
]

# Marathi-specific Devanagari keywords
# Matches any contiguous run of Indian-script characters (one "script word")
_SCRIPT_WORD_RE = re.compile(
    r"[\u0900-\u097F"   # Devanagari
    r"\u0980-\u09FF"    # Bengali
    r"\u0C00-\u0C7F"    # Telugu
    r"\u0B80-\u0BFF"    # Tamil
    r"\u0A80-\u0AFF"    # Gujarati
    r"\u0C80-\u0CFF"    # Kannada
    r"\u0A00-\u0A7F"    # Gurmukhi
    r"\u0D00-\u0D7F"    # Malayalam
    r"\u0B00-\u0B7F"    # Odia
    r"]+"
)

_MARATHI_KEYWORDS = re.compile(
    r"\b(आहे|नाही|माझ्या|तुमच्या|मला|तुम्हाला|आपण|होय)\b"
)

# ── Explicit language-switch request detection ────────────────────────────────
# Catches: "can we talk in hindi", "speak Tamil", "hindi mein baat karo", etc.
_EXPLICIT_SWITCH_RE = re.compile(
    r"""
    (?:
        let['']?s?\s+(?:talk|speak|chat|switch)|
        (?:can\s+(?:you|we)\s+)?(?:speak|talk|reply|respond|answer|continue)\s+(?:in\s+|using\s+)?|
        switch(?:ing)?\s+to\s+|
        (?:please\s+)?(?:use|speak|talk)\s+(?:in\s+)?|
        (?:in|using)\s+|
        baat\s+kar|
        (?:ab|now)\s+\w*\s*(?:mein|में)|
        mujhe\s+\w+\s+(?:mein|में)
    )?
    \b(
        hindi|हिंदी|हिन्दी|
        bengali|bangla|বাংলা|
        telugu|తెలుగు|
        marathi|मराठी|
        tamil|தமிழ்|
        gujarati|ગુજરાતી|
        kannada|ಕನ್ನಡ|
        punjabi|ਪੰਜਾਬੀ|
        malayalam|മലയാളം|
        odia|odiya|ଓଡ଼ିଆ|
        english|अंग्रेजी
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

_NAME_TO_CODE: dict[str, str] = {
    "hindi": "hi",    "हिंदी": "hi",   "हिन्दी": "hi",
    "bengali": "bn",  "bangla": "bn",  "বাংলা": "bn",
    "telugu": "te",   "తెలుగు": "te",
    "marathi": "mr",  "मराठी": "mr",
    "tamil": "ta",    "தமிழ்": "ta",
    "gujarati": "gu", "ગુજરાતી": "gu",
    "kannada": "kn",  "ಕನ್ನಡ": "kn",
    "punjabi": "pa",  "ਪੰਜਾਬੀ": "pa",
    "malayalam": "ml","മലയാളം": "ml",
    "odia": "or",     "odiya": "or",   "ଓଡ଼ିଆ": "or",
    "english": "en",  "अंग्रेजी": "en",
}

# ── Hinglish (Roman-script Hindi) keyword detection ───────────────────────────
# When Deepgram transcribes spoken Hindi BEFORE the connection swap,
# it returns Roman transliteration. These words are unmistakably Hindi.
# Threshold: 2+ matches in a single utterance = Hindi.
# Only truly unambiguous Hindi-only words that cannot appear in English sentences.
# Removed: please, main, se, par, ko, ka, ki, ke, ye, wo — all valid English words.
_HINGLISH_WORDS = re.compile(
    r"\b("
    # Greetings — clearly Hindi, no English equivalent
    r"namaste|namaskar|"
    # Negation — "nahi/nahin" have no English homophones
    r"nahi|nahin|"
    # Question words — distinctly Hindi
    r"kya|kaisa|kaise|kyun|kyon|"
    # Pronouns — "aap/tum" are unambiguous; "hum" removed (English "hum")
    r"aap|tum|"
    # "hain" kept (distinct from English); "hai" removed (→ "hi"), "haan" removed (→ "ha")
    r"hain|"
    # Conjunctions — "lekin" only; "aur" removed (→ "or")
    r"lekin|"
    # Demonstratives — "woh" kept; "yeh" removed (→ "yeah"), "wah" removed (→ "whoa/wow")
    r"woh|"
    # Verbs — all clearly Hindi
    r"karo|karna|karein|karte|"
    r"baat|bolna|bolo|suno|"
    # Affirmatives / filler — all clearly Hindi
    r"theek|thik|accha|acha|bilkul|"
    r"bahut|bohot|zyada|"
    # Possessives — all clearly Hindi
    r"mujhe|mera|meri|mere|"
    r"aapka|aapki|tumhara|"
    # Courtesy words
    r"shukriya|dhanyawad|"
    # Currency / transport
    r"paise|rupaye|rupaya|"
    r"gaadi|gadi|"
    # Imperative verbs
    r"batao|bataiye|samjho"
    r")\b",
    re.IGNORECASE,
)
# Require 2 unambiguous Hindi keywords before switching.
# Threshold=1 caused false positives: "hi" → "hai", "or" → "aur", "yeah" → "yeh".
_HINGLISH_THRESHOLD = 2


SUPPORTED_LANGUAGES: set[str] = {"hi", "bn", "te", "mr", "ta", "gu", "kn", "pa", "ml", "or", "en"}

# ── Data-input detection (skip language switching for reg numbers, phones) ────
_DATA_INPUT_RE = re.compile(r'^[\dA-Za-z\s\.\-\/]+$')


def _is_data_input(text: str) -> bool:
    """True if text looks like a vehicle registration or phone number (language-neutral)."""
    clean = text.strip().rstrip('.')
    no_space = clean.replace(' ', '').replace('-', '').replace('/', '')
    # Purely numeric (phone numbers)
    if no_space.isdigit() and len(no_space) >= 3:
        return True
    # Short alphanumeric with at least one digit (reg numbers like RJ01CA, MH14AB1234)
    if len(no_space) <= 15 and no_space.isalnum() and any(c.isdigit() for c in no_space):
        return True
    return False


def detect_explicit_language_request(text: str) -> Optional[str]:
    """Returns language code if user explicitly asked to switch language."""
    m = _EXPLICIT_SWITCH_RE.search(text)
    if m:
        name = m.group(1).lower()
        code = _NAME_TO_CODE.get(name)
        if code:
            logger.debug(f"Explicit language request: '{name}' → {code}")
            return code
    return None


def detect_language(text: str) -> str:
    """
    Detect language of text. Returns a language code or 'en'.
    Order: script check → Hinglish keyword check → fallback English.
    """
    if not text or not text.strip():
        return "en"

    # 1. Unicode script (fastest, most reliable)
    for pattern, lang_code in _SCRIPT_RANGES:
        if pattern.search(text):
            if lang_code == "hi" and _MARATHI_KEYWORDS.search(text):
                return "mr"
            return lang_code

    # 2. Hinglish — Roman-script Hindi words
    matches = _HINGLISH_WORDS.findall(text.lower())
    if len(matches) >= _HINGLISH_THRESHOLD:
        logger.debug(f"Hinglish detected ({len(matches)} keywords): {matches[:5]}")
        return "hi"

    return "en"


def is_script_based(text: str) -> bool:
    """
    True only when the text contains enough Indian script to indicate a genuine
    language switch — NOT merely a name or single word transcribed in script.

    Two gates must both pass:

    Gate 1 — character ratio:
      Script characters must be ≥ 40% of non-whitespace characters.
      Filters "My name is सौरभ." (≈ 27% script → False).

    Gate 2 — distinct script-word count:
      At least 2 *distinct* script words must be present.
      Filters "वर्शा, I said वर्शा." (same name twice → 1 unique word → False).
      Real Hindi sentences have multiple distinct words:
        "मेरा नाम सौरभ है" → 4 distinct words → True.
    """
    has_script = any(p.search(text) for p, _ in _SCRIPT_RANGES)
    if not has_script:
        return False

    non_space = text.replace(" ", "")
    if not non_space:
        return False

    # Gate 1: character ratio
    script_chars = sum(
        1 for ch in non_space
        if any(p.match(ch) for p, _ in _SCRIPT_RANGES)
    )
    if script_chars / len(non_space) < 0.40:
        return False

    # Gate 2: at least 2 distinct script words
    distinct_script_words = set(_SCRIPT_WORD_RE.findall(text))
    return len(distinct_script_words) >= 2


class LanguageTracker:
    """
    Tracks language across a conversation.

    Switch policy: INTENT-ONLY
    --------------------------
    Language changes ONLY when:
      1. User explicitly requests it ("speak in Hindi", "hindi mein baat karo")
      2. User produces non-Latin Indian script characters (unambiguous)

    Does NOT switch on:
      - Hinglish Roman-script keywords (covers code-switching, Indian names)
      - English words inside a Hindi conversation
      - Ambiguous short utterances
    """

    def __init__(self, initial_language: str = "en", confirm_after: int = 2):
        self.current_language: str = initial_language
        # confirm_after kept for API compat but unused in intent-only mode
        self._confirm_after   = confirm_after
        self._candidate       = initial_language
        self._candidate_count = 0

    def update(self, text: str) -> tuple[str, bool]:
        """
        Feed a transcript. Returns (current_language, switched).
        switched=True means the language just changed right now.

        Switch rules (INTENT-ONLY):
          1. Explicit phrase ("let's talk in Hindi", "english mein baat karo")
             → switch immediately
          2. Non-Latin Indian script detected (Devanagari, Tamil, Telugu etc.)
             → switch immediately (user is clearly writing in that language)

        Everything else keeps the current language:
          - Indian names in Roman script won't trigger a switch
          - Hinglish code-switching won't trigger a switch
          - English words inside Hindi speech won't flip back to English
        """
        # Priority 0: skip detection for data inputs (phone numbers, IDs)
        if _is_data_input(text):
            logger.debug(f"Data input, skipping lang detection: '{text}'")
            return self.current_language, False

        # Priority 1: explicit language-switch request from the user
        explicit = detect_explicit_language_request(text)
        if explicit and explicit != self.current_language:
            logger.info(
                f"Explicit language switch request: {self.current_language} → {explicit} "
                f"| text='{text[:60]}'"
            )
            return self._switch(explicit)

        # Priority 2: non-Latin Indian script — unambiguously the user
        # is using that language (they typed Devanagari, Tamil, etc.).
        if is_script_based(text):
            detected = detect_language(text)
            if detected != self.current_language:
                logger.info(
                    f"Script-based language switch: {self.current_language} → {detected} "
                    f"| text='{text[:60]}'"
                )
                return self._switch(detected)

        # Everything else: stay in current language
        # (covers Hinglish, English words in Hindi speech, Indian names, etc.)
        return self.current_language, False

    def _switch(self, lang: str) -> tuple[str, bool]:
        old = self.current_language
        self.current_language = lang
        self._candidate       = lang
        self._candidate_count = 0
        logger.info(f"Language switched: {old} → {lang}")
        return lang, True