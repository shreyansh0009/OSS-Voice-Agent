"""
CartesiaTTS — Cartesia Sonic-3 (and sonic-2 fallback), AudioSocket-compatible.
 
Architecture
------------
Sonic-3 is WebSocket-only. We support two modes:
 
  PERSISTENT (default for sonic-3)
  ---------------------------------
  One WS connection is opened per call via `call_connection()` and kept alive
  for every utterance. Each sentence creates a new *context* on the shared
  connection. WS handshake cost (~400-600ms) is paid once per call.
  TTFC per utterance ≈ 100-250ms.
 
  ONE-SHOT (fallback / per-utterance)
  ------------------------------------
  `stream_synthesize()` opens a fresh WS connection for each utterance.
  Used when the pipeline can't manage a per-call connection.
  TTFC per utterance ≈ 500-700ms.
 
Audio format : pcm_s16le at 8000 Hz — matches Asterisk AudioSocket (slin16).
Model        : sonic-3  (best quality + lowest latency in Cartesia's lineup)
               Falls back to sonic-2 SSE if CARTESIA_MODEL != sonic-3.
 
Env vars
--------
CARTESIA_API_KEY        required
CARTESIA_VOICE_ID       default: a0e99841-438c-4a64-b679-ae501e7d6091
CARTESIA_MODEL          default: sonic-3  (change to sonic-2 to use SSE path)
"""
from __future__ import annotations

import asyncio
import base64
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import audioop
import httpx

from providers.tts.base import BaseTTS
 
logger = logging.getLogger(__name__)
 
_BASE    = "https://api.cartesia.ai"
_VERSION = "2024-06-10"
_DEFAULT_VOICE = "a0e99841-438c-4a64-b679-ae501e7d6091"

# Request 8kHz directly — matches AudioSocket slin16 and SIP G.711 limit.
# No resampling step needed; Cartesia quality at 8kHz is sufficient for G.711.
_TTS_SOURCE_RATE = 8000
 
# Map internal lang codes → Cartesia BCP-47 language codes
_LANG_MAP: dict[str, str] = {
    "hi": "hi",  "bn": "bn",  "te": "te",  "mr": "hi",  "ta": "ta",
    "gu": "gu",  "kn": "kn",  "pa": "pa",  "ml": "ml",  "or": "or",
    "en": "en",
}
def _clean_hindi_text(text: str) -> str:
    """Remove Devanagari script — force romanized Hinglish for English TTS model."""
    import re
    # Devanagari unicode block: U+0900–U+097F
    text = re.sub(r'[\u0900-\u097F]+', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()
 
 
# ── Per-call persistent connection session ────────────────────────────────────
 
class CartesiaCallSession:
    """
    Wraps a live Cartesia AsyncTTSResourceConnection for the duration of a
    single call.
 
    Each call to ``stream_synthesize`` opens a *new context* on the same
    underlying WebSocket — so the WS handshake is paid only once per call.
 
    Language changes (via ``set_language``) take effect on the next
    ``stream_synthesize`` call with no re-connection needed.
    """
 
    def __init__(
        self,
        connection,          # AsyncTTSResourceConnection from the Cartesia SDK
        parent: "CartesiaTTS",
    ):
        self._conn   = connection
        self._parent = parent   # read voice_id, model, sample_rate, _language from parent
 
    def set_language(self, lang_code: str) -> None:
        """Delegate to the parent TTS so both stay in sync."""
        self._parent.set_language(lang_code)
 
    async def stream_synthesize(self, text: str) -> AsyncIterator[bytes]:
        """
        Stream TTS audio for ``text`` using the shared WS connection.
 
        Creates a fresh context for this utterance, pushes the full text,
        signals no_more_inputs, and yields PCM chunks as they arrive.
        """
        lang     = _LANG_MAP.get(self._parent._language, "en")
        voice    = {"mode": "id", "id": self._parent.voice_id}
        out_fmt  = {
            "container":   "raw",
            "encoding":    "pcm_s16le",
            "sample_rate": _TTS_SOURCE_RATE,   # generate at 16kHz, resample below
        }
 
        logger.info(
            f"Cartesia WS (persistent) | voice={self._parent.voice_id} "
            f"| lang={lang} | model={self._parent.model} | text='{text[:50]}'"
        )
        total_bytes = 0
        resample_state = None  # carried across chunks within this utterance

        try:
            ctx = self._conn.context(
                model_id=self._parent.model,
                voice=voice,
                output_format=out_fmt,
                language=lang,
            )

            await ctx.push(text)
            await ctx.no_more_inputs()

            async for response in ctx.receive():
                if response.type == "chunk" and response.audio:
                    chunk, resample_state = self._parent._resample(response.audio, resample_state)
                    total_bytes += len(chunk)
                    yield chunk
                elif response.type == "done":
                    break

        except asyncio.CancelledError:
            raise  # barge-in — let it propagate
        except Exception as exc:
            logger.error(f"Cartesia persistent WS error: {exc}")
            raise

        logger.info(f"Cartesia WS (persistent) done — {total_bytes} bytes")
 
 
# ── Main TTS provider ─────────────────────────────────────────────────────────
 
class CartesiaTTS(BaseTTS):
    """
    Cartesia TTS provider supporting:
 
      • sonic-3 via persistent per-call WebSocket  (default, lowest latency)
      • sonic-3 via one-shot WebSocket per utterance  (fallback)
      • sonic-2 via SSE HTTP streaming              (legacy)
    """
 
    def __init__(
        self,
        api_key: str,
        voice_id: str = _DEFAULT_VOICE,
        model: str = "sonic-3",
        sample_rate: int = 8000,
        **_ignored,
    ):
        self.api_key     = api_key.strip()
        self.voice_id    = voice_id.strip()
        self.model       = model.strip()
        self.sample_rate = sample_rate
        self._language   = "en"
 
        self._async_cartesia_cls = None
        if self.model == "sonic-3":
            try:
                from cartesia import AsyncCartesia  # type: ignore
                self._async_cartesia_cls = AsyncCartesia
                logger.info("CartesiaTTS: sonic-3 WebSocket mode ready")
            except ImportError:
                logger.warning(
                    "cartesia[websockets] not installed — falling back to sonic-2 SSE. "
                    "Run: pip install 'cartesia[websockets]'"
                )
                self.model = "sonic-2"
 
    # ── Language management ────────────────────────────────────────────────
 
    def set_language(self, lang_code: str) -> bool:
        mapped = _LANG_MAP.get(lang_code, "en")
        if mapped == self._language:
            return False
        old = self._language
        self._language = mapped
        logger.info(f"Cartesia language: {old} → {mapped}")
        return True
 
    # ── Resampling ────────────────────────────────────────────────────────
    def _resample(self, pcm: bytes, state):
        """
        Downsample slin16 PCM from _TTS_SOURCE_RATE to self.sample_rate if needed,
        then apply gentle amplitude softening for phone line output.

        ``state`` must be threaded through successive calls within the same
        synthesis stream so audioop can carry fractional-sample state across
        chunk boundaries.  Pass None at the start of each new utterance.
        Returns (processed_bytes, new_state).
        """
        if _TTS_SOURCE_RATE != self.sample_rate:
            pcm, state = audioop.ratecv(
                pcm, 2, 1, _TTS_SOURCE_RATE, self.sample_rate, state
            )
        # Gentle −2 dB softening: reduces harshness on phone/G.711 line
        pcm = audioop.mul(pcm, 2, 0.80)
        return pcm, state

    # ── Persistent per-call connection ─────────────────────────────────────
 
    @asynccontextmanager
    async def call_connection(self):
        """
        Open a persistent Cartesia WebSocket for the duration of a call.
 
        Usage in the pipeline::
 
            async with self.tts.call_connection() as session:
                # session is a CartesiaCallSession
                async for chunk in session.stream_synthesize(text):
                    ...
 
        The WS is kept alive across all utterances in the call.
        Only available when model == "sonic-3" and the cartesia SDK is installed.
        """
        if self._async_cartesia_cls is None:
            raise RuntimeError("call_connection() requires sonic-3 + cartesia[websockets]")
 
        logger.info("Cartesia: opening persistent WS for call")
        async with self._async_cartesia_cls(api_key=self.api_key) as client:
            async with client.tts.websocket_connect() as connection:
                session = CartesiaCallSession(connection, parent=self)
                logger.info("Cartesia: persistent WS ready")
                try:
                    yield session
                finally:
                    logger.info("Cartesia: persistent WS closed")
 
    # ── Public TTS API ─────────────────────────────────────────────────────
 
    async def synthesize(self, text: str) -> bytes:
        """Non-streaming full synthesis."""
        chunks = []
        async for chunk in self.stream_synthesize(text):
            chunks.append(chunk)
        return b"".join(chunks)
 
    async def stream_synthesize(self, text: str, language_code: str | None = None) -> AsyncIterator[bytes]:
        """
        One-shot streaming synthesis (new WS connection per utterance).
 
        Prefer using a ``CartesiaCallSession`` from ``call_connection()``
        for minimum latency during calls.
        """
        if self.model == "sonic-3" and self._async_cartesia_cls is not None:
            async for chunk in self._stream_ws_oneshot(text):
                yield chunk
        else:
            async for chunk in self._stream_sse(text):
                yield chunk
 
    # ── sonic-3 one-shot WS path ───────────────────────────────────────────
 
    async def _stream_ws_oneshot(self, text: str) -> AsyncIterator[bytes]:
        """Open a fresh WS per utterance (used when no persistent session exists)."""
        lang    = _LANG_MAP.get(self._language, "en")
        voice   = {"mode": "id", "id": self.voice_id}
        out_fmt = {
            "container":   "raw",
            "encoding":    "pcm_s16le",
            "sample_rate": _TTS_SOURCE_RATE,   # generate at 16kHz, resample below
        }
 
        logger.info(
            f"Cartesia WS (one-shot) | voice={self.voice_id} | lang={lang} "
            f"| model={self.model} | text='{text[:50]}'"
        )
        total_bytes = 0
        resample_state = None  # carried across chunks within this utterance

        try:
            async with self._async_cartesia_cls(api_key=self.api_key) as client:
                async with client.tts.websocket_connect() as connection:
                    ctx = connection.context(
                        model_id=self.model,
                        voice=voice,
                        output_format=out_fmt,
                        language=lang,
                    )
                    await ctx.push(text)
                    await ctx.no_more_inputs()

                    async for response in ctx.receive():
                        if response.type == "chunk" and response.audio:
                            chunk, resample_state = self._resample(response.audio, resample_state)
                            total_bytes += len(chunk)
                            yield chunk
                        elif response.type == "done":
                            break

 
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Cartesia one-shot WS error: {exc}")
            raise
 
        logger.info(f"Cartesia WS (one-shot) done — {total_bytes} bytes")
 
    # ── sonic-2 SSE fallback ───────────────────────────────────────────────
 
    @property
    def _headers(self) -> dict:
        return {
            "X-API-Key":        self.api_key,
            "Cartesia-Version": _VERSION,
            "Content-Type":     "application/json",
        }
 
    def _sse_body(self, text: str) -> dict:
        return {
            "model_id":   self.model,
            "transcript": text,
            "voice":      {"mode": "id", "id": self.voice_id},
            "language":   _LANG_MAP.get(self._language, "en"),
            "output_format": {
                "container":   "raw",
                "encoding":    "pcm_s16le",
                "sample_rate": _TTS_SOURCE_RATE,   # generate at 16kHz, resample below
            },
        }
 
    async def _stream_sse(self, text: str) -> AsyncIterator[bytes]:
        """Legacy SSE path for sonic-2."""
        import json
 
        url = f"{_BASE}/tts/sse"
        logger.info(
            f"Cartesia SSE stream | voice={self.voice_id} | lang={self._language} "
            f"| model={self.model} | text='{text[:50]}'"
        )
        total_bytes = 0
        resample_state = None  # carried across chunks within this utterance
        async with httpx.AsyncClient(timeout=30) as client:
            async with client.stream(
                "POST", url,
                headers={**self._headers, "Accept": "text/event-stream"},
                json=self._sse_body(text),
            ) as resp:
                if resp.status_code != 200:
                    err = await resp.aread()
                    logger.error(f"Cartesia SSE FAILED {resp.status_code}: {err[:300]}")
                    resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        event = json.loads(payload)
                    except Exception:
                        continue
                    if event.get("type") == "done" or event.get("done"):
                        break
                    chunk_b64 = event.get("data")
                    if chunk_b64:
                        chunk, resample_state = self._resample(
                            base64.b64decode(chunk_b64), resample_state
                        )
                        total_bytes += len(chunk)
                        yield chunk
        logger.info(f"Cartesia SSE done — {total_bytes} bytes")