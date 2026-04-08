"""
DeepgramSTT — AudioSocket-compatible streaming speech-to-text, multilingual.

AudioSocket sends slin16 PCM (linear16) audio at 8kHz.
Deepgram accepts linear16 8kHz natively — no local re-encoding needed.

Multilingual support
--------------------
nova-2 supports Hindi (hi), Bengali (bn), Tamil (ta), Telugu (te),
Marathi (mr), Gujarati (gu), Kannada (kn), Punjabi (pa), Malayalam (ml).

For multilingual auto-detect: use model=nova-2 with NO language param +
detect_language=true. Deepgram will detect and transcribe accordingly.

For pinned language (after user switches): use model=nova-2 + language=hi.
This gives the best accuracy for that specific language.

Short-word detection strategy:
  PATH A — speech_final=True fires  -> emit immediately
  PATH C — is_final=True but speech_final=False -> start 800ms force-emit timer
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, Optional

import websockets

logger = logging.getLogger(__name__)

FORCE_EMIT_TIMEOUT = 0.4   # seconds — reduced for snappier response


class DeepgramConnection:
    """Active connection to Deepgram."""

    def __init__(
        self,
        ws,
        on_transcript: Callable[[str], Awaitable[None]],
        on_speech_start: Optional[Callable[[], Awaitable[None]]] = None,
    ):
        self._ws = ws
        self._on_transcript = on_transcript
        self._on_speech_start = on_speech_start
        self._pending: str | None = None
        self._force_task: asyncio.Task | None = None
        self._receive_task: asyncio.Task | None = None

    async def send_audio(self, mulaw_bytes: bytes) -> None:
        try:
            await self._ws.send(mulaw_bytes)
        except Exception as e:
            logger.warning(f"Deepgram send_audio error (WS may be closed): {e}")

    async def close(self) -> None:
        if self._force_task and not self._force_task.done():
            self._force_task.cancel()
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
        try:
            await self._ws.close()
        except Exception:
            pass

    def _start_receive(self) -> None:
        self._receive_task = asyncio.create_task(self._receive_loop())

    async def _receive_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                await self._handle(msg)
        except Exception as e:
            logger.warning(f"Deepgram receive loop ended unexpectedly: {e}")

    async def _handle(self, msg: dict) -> None:
        typ = msg.get("type", "")

        if typ == "SpeechStarted":
            if self._on_speech_start:
                await self._on_speech_start()

        elif typ == "Results":
            alts = msg.get("channel", {}).get("alternatives", [])
            text = alts[0].get("transcript", "").strip() if alts else ""
            is_final     = msg.get("is_final", False)
            speech_final = msg.get("speech_final", False)

            if not text:
                return

            if is_final and speech_final:
                self._cancel_force_emit()
                self._pending = None
                await self._emit(text)
            elif is_final:
                self._pending = text
                self._arm_force_emit(text)

        elif typ == "Error":
            logger.error(f"Deepgram error: {msg}")

    def _arm_force_emit(self, text: str) -> None:
        self._cancel_force_emit()

        async def _timer():
            await asyncio.sleep(FORCE_EMIT_TIMEOUT)
            if self._pending == text:
                self._pending = None
                await self._emit(text)

        self._force_task = asyncio.create_task(_timer())

    def _cancel_force_emit(self) -> None:
        if self._force_task and not self._force_task.done():
            self._force_task.cancel()
        self._force_task = None

    async def _emit(self, text: str) -> None:
        if self._on_transcript:
            try:
                await self._on_transcript(text)
            except Exception as e:
                logger.error(f"on_transcript callback error: {e}")


class DeepgramSTT:
    """
    Manages Deepgram WebSocket connection.

    Usage:
        async with stt.connect(on_transcript=my_async_fn) as conn:
            await conn.send_audio(mulaw_bytes)
    """

    def __init__(
        self,
        api_key: str = "",
        model: str = "nova-3",       # FIX: nova-2-general does NOT exist
        language: str = "",           # empty = auto-detect (detect_language=true)
        endpointing_ms: int = 150,
        **kwargs,
    ):
        self._api_key       = api_key or os.getenv("DEEPGRAM_API_KEY", "")
        self._model         = model or "nova-3"
        # empty / "multi" / "auto" all mean: let Deepgram auto-detect
        self._language      = language if language and language.lower() not in ("multi", "auto") else ""
        self._endpointing_ms = endpointing_ms or 150

    def _build_url(self) -> str:
        url = (
            "wss://api.deepgram.com/v1/listen"
            f"?model={self._model}"
            "&encoding=linear16"
            "&sample_rate=8000"
            "&channels=1"
            f"&endpointing={self._endpointing_ms}"
            "&interim_results=true"
            "&smart_format=true"
            "&no_delay=true"
            "&vad_events=true"
        )
        if self._language:
            # Pinned language — best accuracy for that language
            url += f"&language={self._language}"
        else:
            # No language → use multi for multilingual support
            url += "&language=multi"
        return url

    @asynccontextmanager
    async def connect(
        self,
        on_transcript: Callable[[str], Awaitable[None]],
        on_speech_start: Optional[Callable[[], Awaitable[None]]] = None,
    ):
        url     = self._build_url()
        headers = {"Authorization": f"Token {self._api_key}"}
        logger.info(f"Deepgram connecting: model={self._model} lang={self._language or 'auto'}")
        ws   = await asyncio.wait_for(
            websockets.connect(
                url,
                additional_headers=headers,
                ping_interval=10,   # keep-alive every 10s — prevents mid-call drop
                ping_timeout=20,
            ),
            timeout=10.0,  # fail fast if Deepgram is unreachable
        )
        conn = DeepgramConnection(ws, on_transcript, on_speech_start)
        conn._start_receive()
        try:
            yield conn
        finally:
            await conn.close()
            logger.info("Deepgram connection closed")