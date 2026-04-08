"""
StreamingPipeline — Asterisk AudioSocket + Deepgram, multilingual.

Audio format: slin16 PCM (signed 16-bit LE, 8 kHz, mono).

Barge-In Architecture (AudioSocket — no AEC)
---------------------------------------------
AudioSocket has NO echo cancellation. Our TTS audio echoes back through
the caller's phone mic → Asterisk → us. This makes barge-in detection
non-trivial.

Strategy:
  1. While agent speaks: audio is NOT sent to Deepgram (echo prevention).
     Instead, each frame goes into a ring buffer (last 500ms) and its RMS
     energy is measured.
  2. Adaptive energy threshold: we track the average echo energy during TTS
     playback. Real user speech (talking over the agent) is significantly
     louder than echo. Threshold = max(floor, echo_baseline × 2.5).
  3. When energy exceeds threshold for 5+ consecutive frames (100ms), we
     confirm barge-in: cancel TTS, flush the ring buffer to Deepgram
     (so it gets the START of what the user said), and switch to listening.
  4. While agent is processing (LLM thinking, no TTS playing): audio flows
     normally to Deepgram. Transcript-based barge-in is active.
  5. Post-speech cooldown: 400ms after TTS ends, short transcripts are
     ignored (echo tail protection).

Multilingual Deepgram hot-swap:
  When user switches language, we swap the Deepgram connection. During
  the swap (~300-600ms), audio is buffered in _ConnHolder and flushed
  to the new connection once ready.
"""
from __future__ import annotations

import asyncio
import logging
import re
import struct
import time
from collections import deque
from dataclasses import dataclass, field

import websockets

from core.orchestrator import Orchestrator
from core.session import CallSession
from core.latency_logger import get_latency_logger
from providers.stt.deepgram import DeepgramSTT, DeepgramConnection
from providers.tts.base import BaseTTS
from providers.llm.base import BaseLLM

logger = logging.getLogger(__name__)
_lat = get_latency_logger()  # module-level singleton — no overhead per call

# ── AudioSocket protocol constants ──────────────────────────────────────────
AS_TYPE_UUID = 0x00
AS_TYPE_SLIN = 0x10
AS_TYPE_HANGUP = 0xFF

_AS_FRAME_BYTES = 320    # 20ms of slin16 at 8kHz (160 samples × 2 bytes)
_FRAME_MS = 20           # ms per frame
_SILENCE_FRAME = b'\x00' * _AS_FRAME_BYTES

# Silence padding around each TTS turn
_LEAD_SILENCE_FRAMES = 5     # 100ms — primes Asterisk jitter buffer
_TRAIL_SILENCE_FRAMES = 15   # 300ms — lets last word fully play out

_SENTENCE_RE = re.compile(r'(?<=[.!?])\s+')
_CONTROL_RE = re.compile(r'\[(?:HANDOFF|END_CALL|MCP|TOOL):[^\]]*\]|\[END_CALL\]')
_DEBOUNCE_MS = 300

# ── Digit-word normaliser ────────────────────────────────────────────────────
# When the LLM confirms a mobile number it spells it out as
# "nine eight seven six five four three two one zero".
# TTS reads each word as a separate token with a long pause → very slow.
# We convert runs of 5+ digit words into paired numeric groups:
#   "nine eight seven six five four three two one zero" → "98 76 54 32 10"
# TTS then reads these as two-digit numbers (ninety-eight, seventy-six…)
# which is ~2× faster and still perfectly intelligible.

_DIGIT_WORD_MAP: dict[str, str] = {
    'zero': '0', 'oh': '0',
    'one': '1', 'two': '2', 'three': '3', 'four': '4', 'five': '5',
    'six': '6', 'seven': '7', 'eight': '8', 'nine': '9',
}

_DIGIT_WORD_SEQ_RE = re.compile(
    r'\b(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)'
    r'(?:[ \t]+(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)){4,}\b',
    re.IGNORECASE,
)


def _normalize_digit_words(text: str) -> str:
    """
    Replace spelled-out digit runs with paired numeric groups.
    Only fires on 5+ consecutive digit words — won't affect normal sentences
    that happen to mention 'one' or 'two'.
    """
    def _to_pairs(m: re.Match) -> str:
        words = m.group(0).lower().split()
        digits = ''.join(_DIGIT_WORD_MAP.get(w, '') for w in words)
        return ' '.join(digits[i:i+2] for i in range(0, len(digits), 2))
    return _DIGIT_WORD_SEQ_RE.sub(_to_pairs, text)


# ── Barge-In Engine constants ───────────────────────────────────────────────
# Ring buffer: stores recent audio frames during speech for Deepgram flush
_BARGE_RING_SIZE = 25          # 25 frames × 20ms = 500ms audio history

# Energy detection — tuned for 8kHz telephone (slin16 via AudioSocket).
# Phone lines compress audio so absolute RMS levels are lower than desktop mic.
# Tune _BARGE_MIN_ENERGY higher if background noise causes false triggers.
_BARGE_CONFIRM_FRAMES = 6     # 6 × 20ms = 120ms sustained speech to confirm barge-in
                               # (was 15/300ms — too long for short words like "stop")
_BARGE_MIN_ENERGY = 800       # absolute floor for 8kHz phone (was 2500 — too high)
_BARGE_ECHO_MULTIPLIER = 2.5  # threshold = max(floor, echo_avg × this)
                               # (was 4.0 — pushed threshold above normal speech level)
_ECHO_WINDOW = 50             # frames to average for echo baseline (~1s)

# Hysteresis: allow this many sub-threshold frames before resetting hot_frames.
_BARGE_HOLD_FRAMES = 3        # 3 × 20ms = 60ms grace gap (was 4/80ms)

# ZCR (zero-crossing rate) gate:
# At 8kHz, voiced speech ZCR is lower than at higher sample rates.
# Low-pitched male voices can be as low as 2–3 crossings/ms at 8kHz.
_BARGE_ZCR_MIN = 2.0          # minimum ZCR per ms (was 4.0 — too high for 8kHz)

# Post-speech cooldown: ignore short transcripts from echo tail
_POST_SPEECH_COOLDOWN_S = 0.4   # seconds after TTS ends
_COOLDOWN_MIN_CHARS = 8         # during cooldown, transcript must be this long

# ── Utterance commit buffer ──────────────────────────────────────────────────
# Deepgram fires speech_final on every 150ms pause, including mid-sentence
# breaths.  Instead of immediately sending each fragment to the LLM, we hold
# for _UTTERANCE_HOLD_MS and accumulate consecutive fragments.  Only after
# that window of silence do we commit the full utterance to the LLM.
#
# Cost: +200ms to response latency.
# Benefit: prevents the agent from speaking after the first word of a sentence.
# Tune higher (300ms) if users speak with longer pauses; lower (100ms) for
# snappier response at the risk of more false-finals.
_UTTERANCE_HOLD_MS = 200


def _rms_energy(pcm: bytes) -> float:
    """RMS energy of a slin16 PCM frame (signed 16-bit LE)."""
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack(f'<{n}h', pcm)
    return (sum(s * s for s in samples) / n) ** 0.5


def _zcr_per_ms(pcm: bytes) -> float:
    """Zero-crossing rate per millisecond of a slin16 PCM frame.

    Voiced human speech: ~5–20 crossings/ms.
    Impulse noise (hammer, bang): near 0 (one large spike then silent).
    Unvoiced fricatives (/s/, /f/): 20–50 but low energy — caught by energy gate.
    """
    n = len(pcm) // 2
    if n < 2:
        return 0.0
    samples = struct.unpack(f'<{n}h', pcm)
    crossings = sum(
        1 for i in range(1, n)
        if (samples[i] >= 0) != (samples[i - 1] >= 0)
    )
    return crossings / _FRAME_MS  # crossings per millisecond


_DG_LANG: dict[str, str] = {
    "hi": "hi", "bn": "bn", "te": "te", "mr": "mr", "ta": "ta",
    "gu": "gu", "kn": "kn", "pa": "pa", "ml": "ml", "or": "or",
    "en": "en-IN",
}

_LANG_INSTRUCTIONS: dict[str, str] = {
    "hi": "You MUST respond only in Hindi (हिंदी). Do not use English unless the user explicitly asks.",
    "bn": "You MUST respond only in Bengali (বাংলা). Do not use English unless the user explicitly asks.",
    "te": "You MUST respond only in Telugu (తెలుగు). Do not use English unless the user explicitly asks.",
    "mr": "You MUST respond only in Marathi (मराठी). Do not use English unless the user explicitly asks.",
    "ta": "You MUST respond only in Tamil (தமிழ்). Do not use English unless the user explicitly asks.",
    "gu": "You MUST respond only in Gujarati (ગુજરાતી). Do not use English unless the user explicitly asks.",
    "kn": "You MUST respond only in Kannada (ಕನ್ನಡ). Do not use English unless the user explicitly asks.",
    "pa": "You MUST respond only in Punjabi (ਪੰਜਾਬੀ). Do not use English unless the user explicitly asks.",
    "ml": "You MUST respond only in Malayalam (മലയാളം). Do not use English unless the user explicitly asks.",
    "or": "You MUST respond only in Odia (ଓଡ଼ିଆ). Do not use English unless the user explicitly asks.",
    "en": "You MUST respond only in English.",
}


# ── AudioSocket frame parser ────────────────────────────────────────────────

class _FrameParser:
    """Buffers incoming TCP bytes and extracts complete AudioSocket frames."""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        """Feed raw TCP bytes, returns list of (type, payload) tuples."""
        self._buf.extend(data)
        frames = []
        while len(self._buf) >= 3:
            frame_type = self._buf[0]
            length = struct.unpack("!H", self._buf[1:3])[0]
            if len(self._buf) < 3 + length:
                break  # incomplete frame, wait for more data
            payload = bytes(self._buf[3:3 + length])
            self._buf = self._buf[3 + length:]
            frames.append((frame_type, payload))
        return frames


def _make_audio_frame(pcm: bytes) -> bytes:
    """Wrap PCM payload in an AudioSocket SLIN frame."""
    header = struct.pack("!BH", AS_TYPE_SLIN, len(pcm))
    return header + pcm


@dataclass
class _TurnState:
    """
    Tracks the state of the current conversation turn.

    State machine:
      IDLE       (processing=F, speaking=F) — mic open, audio → Deepgram
      PROCESSING (processing=T, speaking=F) — LLM thinking, mic still open
      SPEAKING   (processing=T, speaking=T) — TTS playing, mic gated, energy detection active

    Transitions:
      IDLE → PROCESSING      : transcript received, _handle_turn starts
      PROCESSING → SPEAKING  : _speak() begins TTS output
      SPEAKING → IDLE        : TTS finishes normally (+ cooldown)
      SPEAKING → IDLE        : barge-in detected → cancel turn
      PROCESSING → IDLE      : barge-in (transcript during LLM thinking)
    """
    last_text: str = ""
    last_time: float = 0.0
    processing: bool = False
    speaking: bool = False
    call_ended: bool = False
    barge_in: asyncio.Event = field(default_factory=asyncio.Event)
    current_turn: asyncio.Task | None = None

    # Persistent TTS session (e.g. CartesiaCallSession) — set by run_audiosocket_call
    # when the TTS backend supports a per-call connection.  None = use self.tts directly.
    call_tts: object = None

    # Barge-in engine state
    _ring_buffer: deque = field(default_factory=lambda: deque(maxlen=_BARGE_RING_SIZE))
    _hot_frames: int = 0        # consecutive speech-candidate frames
    _hold_frames: int = 0       # sub-threshold frames allowed by hysteresis
    _echo_energies: deque = field(default_factory=lambda: deque(maxlen=_ECHO_WINDOW))
    _echo_baseline: float = 0.0
    _speech_ended_at: float = 0.0
    _spawned_tasks: set = field(default_factory=set)

    # Utterance commit buffer state
    _pending_text: str = ""                  # accumulated transcript fragments
    _hold_task: asyncio.Task | None = None   # fires after _UTTERANCE_HOLD_MS silence

    def track_task(self, task: asyncio.Task) -> None:
        """Register a fire-and-forget task so it can be cancelled on call end."""
        self._spawned_tasks.add(task)
        task.add_done_callback(self._spawned_tasks.discard)

    def cancel_all_tasks(self) -> None:
        """Cancel all tracked fire-and-forget tasks."""
        for task in list(self._spawned_tasks):
            if not task.done():
                task.cancel()

    def reset_barge_engine(self):
        """Reset barge-in engine state at the start of each TTS utterance."""
        self._ring_buffer.clear()
        self._hot_frames = 0
        self._hold_frames = 0
        self._echo_energies.clear()
        self._echo_baseline = 0.0


class _ConnHolder:
    """
    Wraps the active Deepgram connection with audio buffering during swaps.

    States:
      LIVE     — audio goes straight to _conn
      SWAPPING — audio is buffered in _buffer list
      (after swap) — buffer is flushed to new conn, back to LIVE
    """

    def __init__(self, conn: DeepgramConnection):
        self._conn = conn
        self._swapping = False
        self._buffer: list[bytes] = []
        self._lock = asyncio.Lock()

    async def send_audio(self, data: bytes) -> None:
        async with self._lock:
            if self._swapping:
                self._buffer.append(data)
            else:
                await self._conn.send_audio(data)

    async def begin_swap(self) -> None:
        """Call before opening new WS — starts buffering immediately."""
        async with self._lock:
            self._swapping = True
            self._buffer.clear()

    async def finish_swap(self, new_conn: DeepgramConnection) -> None:
        """
        Atomically replace connection and flush buffered audio to new conn.
        Called after new WS is open and ready.
        """
        async with self._lock:
            old_conn = self._conn
            self._conn = new_conn
            self._swapping = False
            buffered = list(self._buffer)
            self._buffer.clear()

        # Flush buffered audio to new connection
        for chunk in buffered:
            await new_conn.send_audio(chunk)

        # Close old connection outside lock
        await asyncio.sleep(0.1)
        try:
            await old_conn.close()
        except Exception:
            pass


class StreamingPipeline:

    def __init__(
        self,
        stt: DeepgramSTT,
        tts: BaseTTS,
        llm: BaseLLM,
        orchestrator: Orchestrator,
        welcome_message: str = "Hello! Thank you for calling. How can I assist you today?",
    ):
        self.stt = stt
        self.tts = tts
        self.orchestrator = orchestrator
        self.welcome_message = welcome_message
        self._swap_lock = asyncio.Lock()

    # ── Entry point — AudioSocket TCP ────────────────────────────────────────

    async def run_audiosocket_call(
        self,
        call_sid: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a call over Asterisk AudioSocket TCP connection."""
        session = self.orchestrator.start_session()
        audio_out: asyncio.Queue = asyncio.Queue()
        state = _TurnState()

        # Seed default language
        _init_lang = session.current_language or "en"
        session.set("language", _init_lang)
        session.set("language_instruction", _LANG_INSTRUCTIONS.get(_init_lang, ""))

        call_start_mono = time.monotonic()
        logger.info(f"[{call_sid}] AudioSocket call started")
        await _lat.separator(call_sid, f"CALL STARTED  sid={call_sid}")
        await _lat.event(call_sid, "CALL_STARTED")

        # Mute mic during startup — prevents noise during Asterisk
        # connection setup from triggering false transcripts.
        # Welcome task sets speaking=False when done.
        state.speaking = True

        conn_holder: _ConnHolder | None = None

        async def on_transcript(text: str):
            if conn_holder is not None:
                await self._on_transcript(text, audio_out, session, call_sid, state, conn_holder)

        async def on_speech_start():
            # Disabled — Asterisk has no echo cancellation.
            # SpeechStarted fires on TTS echo constantly.
            # Barge-in is handled by energy detection + transcript.
            pass

        async def _run_call_inner():
            """Inner coroutine that runs with the persistent TTS WS already open."""
            async with self.stt.connect(on_transcript=on_transcript, on_speech_start=on_speech_start) as initial_conn:
                conn_holder_ref[0] = _ConnHolder(initial_conn)

                welcome_task = asyncio.create_task(
                    self._play_welcome_audiosocket(audio_out, call_sid, state, session=session),
                    name="welcome",
                )
                done, pending = await asyncio.wait(
                    [
                        asyncio.create_task(
                            self._recv_audiosocket(reader, conn_holder_ref[0], audio_out, call_sid, state),
                            name="recv",
                        ),
                        asyncio.create_task(
                            self._send_audiosocket(writer, audio_out, call_sid, state),
                            name="send",
                        ),
                    ],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # ── Cleanup: cancel ALL tasks spawned during this call ──
                state.call_ended = True
                welcome_task.cancel()
                state.cancel_all_tasks()  # kill orphaned _handle_turn / _swap_deepgram
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
                # Give spawned tasks a moment to finish cancellation
                if state._spawned_tasks:
                    remaining = [t for t in state._spawned_tasks if not t.done()]
                    if remaining:
                        await asyncio.gather(*remaining, return_exceptions=True)

        # We need conn_holder accessible both inside _run_call_inner and in the
        # on_transcript closure.  Use a single-element list as a mutable cell.
        conn_holder_ref: list[_ConnHolder | None] = [None]

        # Patch on_transcript to read from conn_holder_ref
        async def on_transcript(text: str):  # type: ignore[no-redef]
            if conn_holder_ref[0] is not None:
                await self._on_transcript(text, audio_out, session, call_sid, state, conn_holder_ref[0])

        # ── Open persistent TTS WebSocket if supported ─────────────────────────
        # CartesiaTTS sonic-3: one WS hand-shake per call instead of per utterance.
        # All other TTS backends fall through to the else branch unchanged.
        if hasattr(self.tts, "call_connection"):
            try:
                async with self.tts.call_connection() as call_session:
                    state.call_tts = call_session
                    logger.info(f"[{call_sid}] Persistent TTS WS open")
                    await _lat.event(call_sid, "TTS_WS_OPEN")
                    await _run_call_inner()
            except Exception:
                logger.exception(f"[{call_sid}] Persistent TTS WS failed — falling back")
                state.call_tts = None
                await _run_call_inner()
        else:
            await _run_call_inner()

        # Save transcript before clearing call state
        from core.transcript_logger import save_transcript
        await save_transcript(call_sid, session, call_start_mono)

        _lat.clear_call(call_sid)
        logger.info(f"[{call_sid}] AudioSocket call ended")
        await _lat.separator(call_sid, f"CALL ENDED    sid={call_sid}")

    # ── Hot-swap Deepgram ─────────────────────────────────────────────────

    async def _swap_deepgram(
        self,
        lang_code: str,
        conn_holder: _ConnHolder,
        audio_out: asyncio.Queue,
        session: CallSession,
        call_sid: str,
        state: _TurnState,
    ) -> None:
        async with self._swap_lock:
            if state.call_ended:
                return

            dg_lang = _DG_LANG.get(lang_code, lang_code)
            logger.info(f"[{call_sid}] Swap starting → {dg_lang}")

            await conn_holder.begin_swap()

            try:
                new_stt = DeepgramSTT(
                    api_key=self.stt._api_key,
                    model=self.stt._model,
                    language=dg_lang,
                    endpointing_ms=self.stt._endpointing_ms,
                )
                url = new_stt._build_url()
                headers = {"Authorization": f"Token {self.stt._api_key}"}
                ws = await websockets.connect(url, additional_headers=headers)

                async def on_transcript(text: str):
                    await self._on_transcript(text, audio_out, session, call_sid, state, conn_holder)

                async def on_speech_start_swap():
                    pass  # Disabled — same echo issue

                new_conn = DeepgramConnection(ws, on_transcript, on_speech_start_swap)
                new_conn._start_receive()

                await conn_holder.finish_swap(new_conn)
                logger.info(f"[{call_sid}] Swap complete → {dg_lang}")

            except Exception:
                logger.exception(f"[{call_sid}] Swap FAILED for {dg_lang} — resuming old conn")
                async with conn_holder._lock:
                    conn_holder._swapping = False
                    conn_holder._buffer.clear()

    # ── Welcome message ───────────────────────────────────────────────────

    async def _play_welcome_audiosocket(
        self,
        audio_out: asyncio.Queue,
        call_sid: str,
        state: _TurnState,
        session: CallSession | None = None,
    ) -> None:
        if not self.welcome_message:
            state.speaking = False
            return

        # Wait for Asterisk to establish audio path
        await asyncio.sleep(0.5)
        if state.call_ended:
            state.speaking = False
            return

        state.barge_in.clear()
        state.reset_barge_engine()

        try:
            for _ in range(_LEAD_SILENCE_FRAMES):
                await audio_out.put(_SILENCE_FRAME)
            await self._tts_to_queue(self.welcome_message, audio_out, call_sid, state, session=session)
            for _ in range(_TRAIL_SILENCE_FRAMES):
                await audio_out.put(_SILENCE_FRAME)
            if session is not None:
                session.add_message("assistant", self.welcome_message)
        except asyncio.CancelledError:
            logger.info(f"[{call_sid}] Welcome cancelled (barge-in)")
        except Exception:
            logger.exception(f"[{call_sid}] Welcome TTS error")
        finally:
            state.speaking = False
            state._speech_ended_at = time.monotonic()
            logger.info(f"[{call_sid}] Welcome done — mic open")

    # ── Receive from AudioSocket ─────────────────────────────────────────

    async def _recv_audiosocket(
        self,
        reader: asyncio.StreamReader,
        conn_holder: _ConnHolder,
        audio_out: asyncio.Queue,
        call_sid: str,
        state: _TurnState,
    ) -> None:
        """
        Reads AudioSocket frames from Asterisk.

        Audio routing depends on state:
          SPEAKING  → frames go to ring buffer + energy detector (not Deepgram)
          otherwise → frames go directly to Deepgram

        On barge-in detection:
          1. Signal barge-in (cancel TTS)
          2. Drain audio output queue
          3. Cancel current turn task
          4. Flush ring buffer to Deepgram (user's speech history)
          5. Resume normal audio forwarding
        """
        parser = _FrameParser()
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break  # connection closed

                for frame_type, payload in parser.feed(data):
                    if frame_type == AS_TYPE_HANGUP:
                        logger.info(f"[{call_sid}] AudioSocket hangup received")
                        return

                    elif frame_type == AS_TYPE_SLIN:
                        if state.call_ended:
                            continue

                        if state.speaking:
                            # Send silence (not actual echo) to Deepgram so its VAD
                            # pipeline stays warm during TTS playback.  If we send no
                            # audio at all, Deepgram goes cold and needs several warm-up
                            # windows before the first post-TTS utterance is recognised.
                            # Real microphone audio goes into the ring buffer so the
                            # energy-based barge-in engine can still work.
                            await conn_holder.send_audio(_SILENCE_FRAME)
                            await self._handle_frame_during_speech(
                                payload, conn_holder, audio_out, call_sid, state
                            )
                        else:
                            # Not speaking — forward all audio to Deepgram
                            state._hot_frames = 0
                            await conn_holder.send_audio(payload)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"[{call_sid}] _recv_audiosocket error")
        finally:
            await audio_out.put(None)

    async def _handle_frame_during_speech(
        self,
        payload: bytes,
        conn_holder: _ConnHolder,
        audio_out: asyncio.Queue,
        call_sid: str,
        state: _TurnState,
    ) -> None:
        """
        Process an audio frame while TTS is playing.

        Three-layer noise rejection:
          1. Energy threshold (adaptive, raised floor) — filters ambient hiss
          2. ZCR (zero-crossing rate) gate — filters impulse noise (hammer, bang)
          3. Sustained confirmation + hysteresis — requires 300ms of speech,
             tolerates natural micro-gaps so one quiet frame doesn’t reset
        """
        # Always buffer for potential flush on barge-in
        state._ring_buffer.append(payload)

        # Measure energy and update echo baseline
        energy = _rms_energy(payload)
        state._echo_energies.append(energy)

        # Compute adaptive threshold from running echo average
        if len(state._echo_energies) >= 5:
            state._echo_baseline = sum(state._echo_energies) / len(state._echo_energies)
        threshold = max(_BARGE_MIN_ENERGY, state._echo_baseline * _BARGE_ECHO_MULTIPLIER)

        # ── Layer 1: energy gate ───────────────────────────────────
        if energy <= threshold:
            # Below energy threshold — apply hysteresis before resetting
            state._hold_frames += 1
            if state._hold_frames > _BARGE_HOLD_FRAMES:
                # Sustained silence: definitely not speech any more
                state._hot_frames = 0
                state._hold_frames = 0
            return

        # ── Layer 2: ZCR (impulse noise) gate ───────────────────────
        # High energy + low ZCR = impulsive transient (hammer, door slam, etc.)
        # Don’t count this toward barge-in confirmation.
        zcr = _zcr_per_ms(payload)
        if zcr < _BARGE_ZCR_MIN:
            logger.debug(
                f"[{call_sid}] Barge-in ZCR reject "
                f"energy={energy:.0f} zcr={zcr:.1f} (< {_BARGE_ZCR_MIN})"
            )
            # Impulse frames don’t reset hot_frames — they’re just skipped
            state._hold_frames = 0
            return

        # ── Layer 3: sustained confirmation + hysteresis ─────────────
        state._hot_frames += 1
        state._hold_frames = 0   # reset silence gap on each hot frame

        if state._hot_frames >= _BARGE_CONFIRM_FRAMES:
            # ── BARGE-IN CONFIRMED ──
            logger.info(
                f"[{call_sid}] BARGE-IN detected | "
                f"hot={state._hot_frames} energy={energy:.0f} "
                f"threshold={threshold:.0f} zcr={zcr:.1f} "
                f"echo_baseline={state._echo_baseline:.0f}"
            )

            # 1. Signal barge-in → stops TTS production
            state.barge_in.set()

            # 2. Drain pending TTS audio from output queue
            _drain(audio_out)

            # 3. Cancel current turn
            if state.current_turn and not state.current_turn.done():
                state.current_turn.cancel()
            state.speaking = False
            state.processing = False

            # 4. Flush ring buffer to Deepgram — gives it the START
            #    of the user’s speech, not just the tail after detection
            buffered = list(state._ring_buffer)
            state._ring_buffer.clear()
            for frame in buffered:
                await conn_holder.send_audio(frame)

            # 5. Reset engine state
            state._hot_frames = 0
            state._hold_frames = 0
            state._echo_energies.clear()
            state._echo_baseline = 0.0
            state._speech_ended_at = 0.0  # no cooldown on barge-in

    # ── Transcript handler ────────────────────────────────────────────────

    async def _on_transcript(
        self,
        text: str,
        audio_out: asyncio.Queue,
        session: CallSession,
        call_sid: str,
        state: _TurnState,
        conn_holder: _ConnHolder,
    ) -> None:
        if state.call_ended:
            return

        # Mark the moment Deepgram sent us a speech_final transcript
        stt_emit_time = time.monotonic()
        await _lat.event(
            call_sid, "USER_STOPPED",
            extra=f"text='{text[:50]}' lang={session.current_language}"
        )

        # ── Debounce duplicate transcripts ──
        now = stt_emit_time
        gap_ms = (now - state.last_time) * 1000
        if text.lower().strip() == state.last_text.lower().strip() and gap_ms < _DEBOUNCE_MS:
            return

        # ── Post-speech echo cooldown ──
        # After TTS ends, echo tail can produce short garbage transcripts.
        # During cooldown window, only accept substantial transcripts.
        if state._speech_ended_at > 0:
            elapsed = now - state._speech_ended_at
            if elapsed < _POST_SPEECH_COOLDOWN_S:
                if len(text.strip()) < _COOLDOWN_MIN_CHARS:
                    logger.info(
                        f"[{call_sid}] Cooldown drop ({elapsed*1000:.0f}ms, "
                        f"{len(text.strip())} chars): '{text}'"
                    )
                    return
            # Past cooldown window — clear the timestamp
            state._speech_ended_at = 0.0

        state.last_text = text
        state.last_time = now
        logger.info(f"[{call_sid}] STT [{session.current_language}]: '{text}'")
        await _lat.event(
            call_sid, "STT_EMIT",
            t0=stt_emit_time,
            extra=f"text='{text[:50]}'"
        )

        # ── Language detection ──
        lang_code, switched = session.update_language(text)
        current_instruction = _LANG_INSTRUCTIONS.get(lang_code, "")
        session.set("language", lang_code)
        session.set("language_instruction", current_instruction)

        if switched:
            logger.info(f"[{call_sid}] Language switched → {lang_code}")
            self.tts.set_language(lang_code)
            swap_task = asyncio.create_task(
                self._swap_deepgram(lang_code, conn_holder, audio_out, session, call_sid, state)
            )
            state.track_task(swap_task)

        # ── Transcript-based barge-in ──
        # Active when processing=True (LLM thinking, mic open, audio → Deepgram).
        # On barge-in we cancel immediately — no hold window — so the user's
        # correction is acted on right away.
        if state.processing:
            logger.info(f"[{call_sid}] Barge-in (transcript during processing): '{text[:40]}'")
            state.barge_in.set()
            _drain(audio_out)
            if state.current_turn and not state.current_turn.done():
                state.current_turn.cancel()
            state.processing = False
            state.speaking = False
            # Cancel any pending hold timer — start fresh with the barge-in text
            if state._hold_task and not state._hold_task.done():
                state._hold_task.cancel()
            state._pending_text = ""
            turn_task = asyncio.create_task(
                self._handle_turn(text, audio_out, session, call_sid, state)
            )
            state.track_task(turn_task)
            return

        # ── Utterance commit buffer ──────────────────────────────────────────
        # Accumulate consecutive Deepgram speech_final fragments that arrive
        # within _UTTERANCE_HOLD_MS of each other.  Only commit to the LLM
        # after a silence gap ≥ _UTTERANCE_HOLD_MS — this prevents mid-sentence
        # pauses from triggering the LLM before the user has finished speaking.
        if state._hold_task and not state._hold_task.done():
            # Another fragment arrived before the window expired — extend.
            state._hold_task.cancel()
            state._pending_text = (state._pending_text + " " + text).strip()
            logger.info(
                f"[{call_sid}] Utterance hold extended: '{state._pending_text[:60]}'"
            )
        else:
            state._pending_text = text

        committed = state._pending_text   # snapshot for the closure

        async def _commit_turn() -> None:
            try:
                await asyncio.sleep(_UTTERANCE_HOLD_MS / 1000)
            except asyncio.CancelledError:
                return  # another fragment arrived — hold was extended
            if not state.call_ended:
                logger.info(f"[{call_sid}] Utterance committed: '{committed[:60]}'")
                t = asyncio.create_task(
                    self._handle_turn(committed, audio_out, session, call_sid, state)
                )
                state.track_task(t)

        hold_task = asyncio.create_task(_commit_turn())
        state._hold_task = hold_task
        state.track_task(hold_task)

    # ── One conversation turn ─────────────────────────────────────────────

    async def _handle_turn(
        self,
        text: str,
        audio_out: asyncio.Queue,
        session: CallSession,
        call_sid: str,
        state: _TurnState,
    ) -> None:
        if state.processing:
            return

        state.processing = True
        my_task = asyncio.current_task()
        state.current_turn = my_task
        state.barge_in.clear()

        turn_start = time.monotonic()
        try:
            llm_start = time.monotonic()
            await _lat.event(call_sid, "LLM_START", t0=turn_start,
                             extra=f"input='{text[:50]}'")
            logger.info(f"[{call_sid}] [LAT] LLM start")

            # ── Streaming LLM → TTS pipeline ──────────────────────────────
            # LLM and TTS run serially (TTS blocks the LLM generator between
            # sentences) to ensure stable 20ms audio pacing. Decoupling them
            # causes event loop contention that disrupts AudioSocket timing.
            # The "LLM done" timer includes TTS time — actual perceived latency
            # is much lower (user hears first sentence while LLM still generates).
            reply_parts: list[str] = []
            final_response = None
            tts_started = False

            async for sentence, resp in self.orchestrator.stream_process(text, session):
                if state.barge_in.is_set():
                    break

                if sentence:
                    if not tts_started:
                        state.reset_barge_engine()
                        state.speaking = True
                        for _ in range(_LEAD_SILENCE_FRAMES):
                            if state.barge_in.is_set():
                                break
                            await audio_out.put(_SILENCE_FRAME)
                        tts_started = True

                    reply_parts.append(sentence)
                    await self._tts_to_queue(sentence, audio_out, call_sid, state,
                                             session=session)

                if resp is not None:
                    final_response = resp
                    llm_ms = (time.monotonic() - llm_start) * 1000
                    full_reply = " ".join(reply_parts)
                    await _lat.event(call_sid, "LLM_DONE", t0=llm_start,
                                     extra=f"llm_ms={llm_ms:.0f} reply='{full_reply[:60]}'")
                    logger.info(
                        f"[{call_sid}] [LAT] LLM done in {llm_ms:.0f}ms | "
                        f"Reply [{session.current_language}]: '{full_reply[:100]}'"
                    )

            # Trail silence
            if not state.barge_in.is_set():
                for _ in range(_TRAIL_SILENCE_FRAMES):
                    if state.barge_in.is_set():
                        break
                    await audio_out.put(_SILENCE_FRAME)

            turn_ms = (time.monotonic() - turn_start) * 1000
            await _lat.event(call_sid, "TURN_COMPLETE", t0=turn_start,
                             extra=f"total_turn_ms={turn_ms:.0f}")
            logger.info(f"[{call_sid}] [LAT] Turn complete in {turn_ms:.0f}ms total")

            if final_response and final_response.end_call:
                state.call_ended = True
                await audio_out.put(None)

        except asyncio.CancelledError:
            logger.info(f"[{call_sid}] Turn cancelled by barge-in")

        except Exception:
            logger.exception(f"[{call_sid}] _handle_turn error")

        finally:
            # Only reset if WE are still the active turn.
            if state.current_turn is my_task:
                if not state.barge_in.is_set():
                    state._speech_ended_at = time.monotonic()
                state.speaking = False
                state.processing = False
                state.current_turn = None

    # ── TTS helpers ───────────────────────────────────────────────────────

    async def _speak(
        self,
        text: str,
        audio_out: asyncio.Queue,
        call_sid: str,
        state: _TurnState,
        session: CallSession | None = None,
    ) -> None:
        clean = _CONTROL_RE.sub("", text).strip()
        if not clean:
            return

        sentences = [s.strip() for s in _SENTENCE_RE.split(clean) if s.strip()] or [clean]

        # Reset barge-in engine for this utterance
        state.reset_barge_engine()
        state.speaking = True  # NOW gate the mic — TTS is about to play

        try:
            # Lead silence
            for _ in range(_LEAD_SILENCE_FRAMES):
                if state.barge_in.is_set():
                    return
                await audio_out.put(_SILENCE_FRAME)

            for sentence in sentences:
                if state.barge_in.is_set():
                    break
                await self._tts_to_queue(sentence, audio_out, call_sid, state, session=session)

            # Trail silence
            for _ in range(_TRAIL_SILENCE_FRAMES):
                if state.barge_in.is_set():
                    return
                await audio_out.put(_SILENCE_FRAME)

        finally:
            state.speaking = False

    async def _tts_to_queue(
        self,
        text: str,
        audio_out: asyncio.Queue,
        call_sid: str,
        state: _TurnState,
        session: CallSession | None = None,
    ) -> None:
        # Convert digit-word runs to paired numerics before TTS sees the text.
        # e.g. "nine eight seven six five four three two one zero" → "98 76 54 32 10"
        text = _normalize_digit_words(text)
        tts_start = time.monotonic()
        # Use persistent call session if available (sonic-3), else fall back
        _tts_source = state.call_tts if state.call_tts is not None else self.tts
        _mode = "persistent" if state.call_tts is not None else "one-shot"
        await _lat.event(call_sid, "TTS_START", t0=tts_start,
                         extra=f"mode={_mode} text='{text[:50]}'")
        logger.info(f"[{call_sid}] [LAT] TTS start ({_mode}): '{text[:60]}'")
        first_chunk = True
        chunk_count = 0
        leftover = b""  # buffer for partial frames across TTS chunks
        try:
            async for chunk in _tts_source.stream_synthesize(text):
                if state.barge_in.is_set():
                    return
                if first_chunk:
                    fc_ms = (time.monotonic() - tts_start) * 1000
                    await _lat.event(call_sid, "TTS_FIRST_CHUNK", t0=tts_start,
                                     extra=f"ttfc_ms={fc_ms:.0f}")
                    logger.info(f"[{call_sid}] [LAT] TTS first chunk in {fc_ms:.0f}ms")
                    first_chunk = False
                chunk_count += 1
                # Prepend any leftover bytes from previous chunk
                data = leftover + chunk
                leftover = b""
                # Split into 320-byte frames (20ms each)
                for i in range(0, len(data), _AS_FRAME_BYTES):
                    if state.barge_in.is_set():
                        return
                    frame = data[i: i + _AS_FRAME_BYTES]
                    if len(frame) < _AS_FRAME_BYTES:
                        # Don't pad mid-stream — carry over to next chunk
                        leftover = frame
                    else:
                        await audio_out.put(frame)

            # Flush any remaining bytes at end of stream (pad only the final frame)
            if leftover:
                frame = leftover + b'\x00' * (_AS_FRAME_BYTES - len(leftover))
                await audio_out.put(frame)

            tts_ms = (time.monotonic() - tts_start) * 1000
            await _lat.event(call_sid, "TTS_DONE", t0=tts_start,
                             extra=f"tts_ms={tts_ms:.0f} chunks={chunk_count}")
            logger.info(f"[{call_sid}] [LAT] TTS done in {tts_ms:.0f}ms ({chunk_count} chunks)")
        except Exception:
            logger.exception(f"[{call_sid}] TTS error: '{text[:60]}'")

    # ── Send to AudioSocket ──────────────────────────────────────────────

    async def _send_audiosocket(
        self,
        writer: asyncio.StreamWriter,
        audio_out: asyncio.Queue,
        call_sid: str,
        state: _TurnState,
    ) -> None:
        """Send audio frames to Asterisk with real-time pacing (20ms/frame)."""
        next_at = 0.0
        frames_since_drain = 0
        while True:
            chunk = await audio_out.get()
            if chunk is None:
                break
            if chunk == "CLEAR":
                continue
            if state.barge_in.is_set():
                continue

            try:
                frame = _make_audio_frame(chunk)
                writer.write(frame)

                # Drain every 10 frames (200ms) instead of every frame.
                # Per-frame drain introduces ~1-5ms jitter × 50 frames/s
                # which compounds into audible glitches and broken words.
                frames_since_drain += 1
                if frames_since_drain >= 10:
                    await writer.drain()
                    frames_since_drain = 0

                # Real-time pacing: 1 frame per 20ms
                now = time.monotonic() * 1000
                if next_at == 0 or now > next_at + _FRAME_MS * 2:
                    next_at = now + _FRAME_MS
                else:
                    delay = next_at - now
                    if delay > 1:
                        await asyncio.sleep(delay / 1000)
                    next_at += _FRAME_MS
            except (ConnectionResetError, BrokenPipeError):
                logger.info(f"[{call_sid}] AudioSocket connection lost")
                break
            except Exception:
                logger.exception(f"[{call_sid}] _send_audiosocket error")
                break


def _drain(q: asyncio.Queue) -> None:
    while not q.empty():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            break
