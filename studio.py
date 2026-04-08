"""
Agent Studio — voice + text browser UI for testing agent prompts.

Speak into your mic, the agent responds with synthesized voice.
Also supports text input for quick testing.

Usage:
    python studio.py                      # Groq LLM + Deepgram STT + Cartesia TTS
    python studio.py --mock               # stub everything, no API keys needed
    python studio.py --no-rag --no-mcp    # skip optional systems
    python studio.py --port 7860

Open http://localhost:7860
"""
from __future__ import annotations

import argparse
import base64
import logging
import os
import struct
import wave
import io

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

load_dotenv()
logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

_orchestrator = None
_session = None
_stt = None
_tts = None


# ── WAV helpers ───────────────────────────────────────────────────────────────

def _pcm_s16le_to_wav(pcm: bytes, sample_rate: int = 24000, channels: int = 1) -> bytes:
    """Wrap raw PCM s16le bytes in a WAV container so browsers can play it."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)  # 16-bit = 2 bytes
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


# ── Mock providers (no API keys) ──────────────────────────────────────────────

class _MockSTT:
    async def transcribe_bytes(self, audio: bytes, content_type: str = "") -> str:
        return "[mock transcript — real STT needs DEEPGRAM_API_KEY or whisper]"


class _MockTTS:
    async def synthesize(self, text: str) -> bytes:
        # Return a tiny valid WAV (0.1s silence) so browser audio works
        silence = bytes(int(24000 * 0.1) * 2)  # 0.1s of s16le silence
        return _pcm_s16le_to_wav(silence, 24000)


class _MockLLM:
    async def chat(self, messages):
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "").lower()
        if any(w in user for w in ["service", "oil", "repair", "appointment", "brake"]):
            return "[HANDOFF:service]"
        if any(w in user for w in ["buy", "car", "rav4", "camry", "test drive", "sales", "inventory"]):
            return "[HANDOFF:sales]"
        if any(w in user for w in ["bye", "thank", "done", "hang up"]):
            return "[END_CALL]"
        return f"[Mock response to: \"{messages[-1]['content']}\"]"

    async def stream_chat(self, messages):
        yield await self.chat(messages)


# ── Real STT: Deepgram REST (single-shot, not streaming) ──────────────────────

class _DeepgramSTT:
    """Use Deepgram's pre-recorded audio endpoint.
    Converts browser WebM/Opus to raw PCM WAV using Python only — no ffmpeg needed.
    """

    def __init__(self, api_key: str, model: str = "nova-2", language: str = ""):
        self.api_key = api_key
        self.model = model
        self.language = language if language and language.lower() not in ("auto", "multi") else ""

    @staticmethod
    def _webm_to_wav(webm_bytes: bytes) -> tuple[bytes, bool]:
        """
        Try to convert WebM/Opus bytes to 16kHz mono WAV using ffmpeg.
        Returns (audio_bytes, is_wav).
        Falls back to sending raw bytes if ffmpeg unavailable.
        """
        import tempfile, subprocess, os, shutil

        # Check ffmpeg exists
        if not shutil.which("ffmpeg"):
            logger.warning("[STT] ffmpeg not found — sending raw WebM to Deepgram")
            return webm_bytes, False

        in_path = out_path = None
        try:
            # Write input
            fd, in_path = tempfile.mkstemp(suffix=".webm")
            with os.fdopen(fd, "wb") as f:
                f.write(webm_bytes)

            # Output path
            out_path = in_path[:-5] + ".wav"

            result = subprocess.run(
                ["ffmpeg", "-y",
                 "-i", in_path,
                 "-ar", "16000",    # 16kHz
                 "-ac", "1",        # mono
                 "-sample_fmt", "s16",  # 16-bit PCM
                 "-f", "wav",
                 out_path],
                capture_output=True,
                timeout=15
            )

            if result.returncode != 0:
                logger.error(f"[STT] ffmpeg failed (rc={result.returncode}): {result.stderr[-300:]}")
                return webm_bytes, False

            with open(out_path, "rb") as f:
                wav_bytes = f.read()

            logger.info(f"[STT] Converted {len(webm_bytes)}B webm → {len(wav_bytes)}B wav")
            return wav_bytes, True

        except Exception as e:
            logger.error(f"[STT] ffmpeg exception: {e}")
            return webm_bytes, False
        finally:
            if in_path and os.path.exists(in_path):
                try: os.unlink(in_path)
                except: pass
            if out_path and os.path.exists(out_path):
                try: os.unlink(out_path)
                except: pass

    async def transcribe_bytes(self, audio: bytes, content_type: str = "audio/webm") -> str:
        import httpx

        if not audio or len(audio) < 100:
            logger.warning(f"[STT] Audio too small ({len(audio) if audio else 0}B), skipping")
            return ""

        logger.info(f"[STT] Received {len(audio)}B, content_type={content_type}")

        # Always try to convert to WAV — most reliable format for Deepgram REST
        audio_data, is_wav = self._webm_to_wav(audio)
        send_ct = "audio/wav" if is_wav else "audio/webm"

        logger.info(f"[STT] Sending {len(audio_data)}B as {send_ct}")

        params = {
            "model": self.model,
            "smart_format": "true",
            "punctuate": "true",
            "diarize": "false",
            "utterances": "false",
            "filler_words": "false",
        }
        if self.language:
            params["language"] = self.language
        else:
            params["detect_language"] = "true"

        headers = {
            "Authorization": f"Token {self.api_key}",
            "Content-Type": send_ct,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.deepgram.com/v1/listen",
                params=params, headers=headers, content=audio_data,
            )
            if resp.status_code != 200:
                logger.error(f"[STT] Deepgram error: {resp.text[:300]}")
                raise RuntimeError(f"Deepgram {resp.status_code}: {resp.text[:200]}")
            data = resp.json()

        transcript = (
            data.get("results", {})
                .get("channels", [{}])[0]
                .get("alternatives", [{}])[0]
                .get("transcript", "")
                .strip()
        )
        logger.info(f"[STT] Transcript: '{transcript[:80]}'")
        return transcript


# ── Real TTS: ElevenLabs Flash v2.5 — mp3_44100 → WAV for browser ────────────

class _ElevenLabsBrowserTTS:
    """
    ElevenLabs TTS with automatic multilingual model switching.

    - English  → eleven_flash_v2_5  (fast, low latency)
    - Hindi / any Indian language → eleven_multilingual_v2 (supports Devanagari + Roman Hindi)

    eleven_flash_v2_5 does NOT support Hindi/Indian languages.
    eleven_multilingual_v2 supports 29 languages including Hindi.
    """

    SAMPLE_RATE = 44100

    # English-only fast model
    _ENGLISH_MODEL = "eleven_flash_v2_5"
    # Multilingual model — supports Hindi, Tamil, Telugu, Bengali, etc.
    _MULTILINGUAL_MODEL = "eleven_multilingual_v2"

    # Languages that require the multilingual model
    _NON_ENGLISH = {"hi", "bn", "te", "mr", "ta", "gu", "kn", "pa", "ml", "or"}

    def __init__(self, api_key: str, voice_id: str, model: str = "eleven_flash_v2_5", speed: float = 1.0):
        self.api_key = api_key
        self.voice_id = voice_id
        self._base_model = model  # user-configured model (used for English)
        self._current_lang = "en"
        self.speed = speed

    @property
    def model(self) -> str:
        """Return correct model for current language."""
        if self._current_lang in self._NON_ENGLISH:
            return self._MULTILINGUAL_MODEL
        return self._base_model

    def set_language(self, lang_code: str) -> None:
        """Called by studio when language switches — auto-selects correct model."""
        self._current_lang = lang_code
        logger.info(f"[TTS] Language set to {lang_code} → using model: {self.model}")

    def _headers(self) -> dict:
        return {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
        }

    def _payload(self, text: str) -> dict:
        return {
            "text": text,
            "model_id": self.model,
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
                "style": 0.0,
                "use_speaker_boost": True,
            },
        }

    async def synthesize(self, text: str) -> bytes:
        """Returns MP3 audio bytes for browser playback."""
        import httpx
        url = (
            f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}"
            f"?output_format=mp3_44100_128"
        )
        logger.info(f"[TTS] synthesize | lang={self._current_lang} model={self.model} chars={len(text)}")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, headers=self._headers(), json=self._payload(text))
            resp.raise_for_status()
            return resp.content  # MP3 bytes — browsers play natively

    async def stream_synthesize(self, text: str):
        """Yields MP3 chunks as they arrive from ElevenLabs."""
        import httpx
        url = (
            f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}/stream"
            f"?output_format=mp3_44100_128"
        )
        logger.info(f"[TTS] stream_synthesize | lang={self._current_lang} model={self.model} chars={len(text)}")
        async with httpx.AsyncClient(timeout=30) as client:
            async with client.stream(
                "POST", url, headers=self._headers(), json=self._payload(text),
            ) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes(chunk_size=4096):
                    if chunk:
                        yield chunk


# ── App factory ───────────────────────────────────────────────────────────────

def create_app(mock: bool, no_rag: bool, no_mcp: bool) -> FastAPI:
    app = FastAPI(title="Agent Studio")

    # ── Providers ─────────────────────────────────────────────────────────────
    global _stt, _tts

    if mock:
        llm = _MockLLM()
        _stt = _MockSTT()
        _tts = _MockTTS()
        llm_label = "Mock"
        stt_label = "Mock"
        tts_label = "Mock (silence)"
    else:
        from config.settings import Settings
        s = Settings.from_env()

        # LLM
        if s.llm_provider == "groq":
            from providers.llm.groq_provider import GroqLLM
            llm = GroqLLM(api_key=s.groq_api_key, model=s.groq_model, temperature=s.llm_temperature)
            llm_label = f"Groq/{s.groq_model}"
        elif s.llm_provider == "litellm":
            from providers.llm.litellm_provider import LiteLLMProvider
            llm = LiteLLMProvider.from_env()
            llm_label = "LiteLLM"
        else:
            from providers.llm.ollama import OllamaLLM
            llm = OllamaLLM(base_url=s.ollama_base_url, model=s.ollama_model, temperature=s.llm_temperature)
            llm_label = f"Ollama/{s.ollama_model}"

        # STT
        if s.stt_provider == "deepgram" and s.deepgram_api_key:
            _stt = _DeepgramSTT(api_key=s.deepgram_api_key, model=s.deepgram_model, language=s.deepgram_language)
            stt_label = f"Deepgram/{s.deepgram_model}"
        else:
            # Whisper via faster-whisper
            try:
                from providers.stt.whisper import WhisperSTT

                class _WhisperAdapter:
                    def __init__(self):
                        self._w = WhisperSTT(model_size=s.whisper_model, device=s.whisper_device, compute_type=s.whisper_compute)
                    async def transcribe_bytes(self, audio: bytes, content_type: str = "") -> str:
                        import tempfile, subprocess
                        # Write audio to temp file; convert to wav via ffmpeg if needed
                        suffix = ".webm" if "webm" in content_type else ".wav"
                        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
                            f.write(audio)
                            tmp_path = f.name
                        wav_path = tmp_path.replace(suffix, ".wav")
                        subprocess.run(["ffmpeg", "-y", "-i", tmp_path, wav_path],
                                       capture_output=True, check=True)
                        segments, _ = self._w._model.transcribe(wav_path, language=self._w._language or None)
                        return " ".join(s.text.strip() for s in segments)

                _stt = _WhisperAdapter()
                stt_label = f"Whisper/{s.whisper_model}"
            except Exception as e:
                logger.warning(f"Whisper not available: {e} — voice disabled")
                _stt = _MockSTT()
                stt_label = "Unavailable"

        # TTS
        if s.tts_provider == "elevenlabs" and s.elevenlabs_api_key:
            _tts = _ElevenLabsBrowserTTS(
                api_key=s.elevenlabs_api_key,
                voice_id=s.elevenlabs_voice_id,
                model=s.elevenlabs_model,
                speed=s.elevenlabs_speed,
            )
            tts_label = f"ElevenLabs/{s.elevenlabs_model}"
        else:
            _tts = _MockTTS()
            tts_label = "Mock (set ELEVENLABS_API_KEY for real voice)"

    # ── Orchestrator ──────────────────────────────────────────────────────────
    if no_rag:
        os.environ["EMBEDDING_PROVIDER"] = "__disabled__"
    if no_mcp:
        os.environ["MCP_SERVERS_PATH"] = "__disabled__"

    from config.settings import Settings
    settings = Settings.from_env()
    from agents.registry import build_orchestrator
    orchestrator = build_orchestrator(settings.squad_path, llm)

    global _orchestrator, _session
    _orchestrator = orchestrator
    _session = orchestrator.start_session()

    # Seed default language instruction (English) so LLM has context from turn 1
    _session.set("language", "en")
    _session.set("language_instruction", "You MUST respond only in English.")

    print(f"  LLM: {llm_label}  |  STT: {stt_label}  |  TTS: {tts_label}")

    # Language instructions — agent ALWAYS mirrors the user's current language.
    # No explicit request needed — if user speaks Hindi, respond in Hindi immediately.
    _LANG_INSTRUCTIONS = {
        "hi": "The user is speaking HINDI. You MUST respond entirely in Hindi (हिंदी). Mirror the user's language automatically — no explicit request needed. Never use English words in your response.",
        "bn": "The user is speaking BENGALI. You MUST respond entirely in Bengali (বাংলা). Mirror the user's language automatically.",
        "te": "The user is speaking TELUGU. You MUST respond entirely in Telugu (తెలుగు). Mirror the user's language automatically.",
        "mr": "The user is speaking MARATHI. You MUST respond entirely in Marathi (मराठी). Mirror the user's language automatically.",
        "ta": "The user is speaking TAMIL. You MUST respond entirely in Tamil (தமிழ்). Mirror the user's language automatically.",
        "gu": "The user is speaking GUJARATI. You MUST respond entirely in Gujarati (ગુજરાતી). Mirror the user's language automatically.",
        "kn": "The user is speaking KANNADA. You MUST respond entirely in Kannada (ಕನ್ನಡ). Mirror the user's language automatically.",
        "pa": "The user is speaking PUNJABI. You MUST respond entirely in Punjabi (ਪੰਜਾਬੀ). Mirror the user's language automatically.",
        "ml": "The user is speaking MALAYALAM. You MUST respond entirely in Malayalam (മലയാളം). Mirror the user's language automatically.",
        "or": "The user is speaking ODIA. You MUST respond entirely in Odia (ଓଡ଼ିଆ). Mirror the user's language automatically.",
        "en": "The user is speaking ENGLISH. You MUST respond entirely in English.",
    }

    def _update_session_language(transcript: str) -> tuple:
        """Run language detection on transcript, update session so LLM gets correct instruction."""
        lang_code, switched = _session.update_language(transcript)
        _session.set("language", lang_code)
        _session.set("language_instruction", _LANG_INSTRUCTIONS.get(lang_code, ""))
        # Always sync TTS language (not just on switch) so model is correct every turn
        if hasattr(_tts, "set_language"):
            _tts.set_language(lang_code)
        if switched:
            logger.info(f"[studio] Language switched -> {lang_code}")
        return lang_code, switched

    # ── API routes ────────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTMLResponse(_UI_HTML
            .replace("{{LLM}}", llm_label)
            .replace("{{STT}}", stt_label)
            .replace("{{TTS}}", tts_label))

    @app.get("/agents")
    async def list_agents():
        return {"agents": list(_orchestrator._agents.keys()), "current": _session.current_agent}

    @app.get("/state")
    async def state():
        mcp = {k[4:]: v for k, v in _session.metadata.items() if k.startswith("mcp_")}
        meta = {k: v for k, v in _session.metadata.items() if not k.startswith("mcp_")}
        return {
            "session_id": _session.session_id,
            "current_agent": _session.current_agent,
            "turn_count": len([m for m in _session.history if m["role"] == "user"]),
            "metadata": meta, "mcp_results": mcp, "history": _session.history,
        }

    @app.post("/voice")
    async def voice(audio: UploadFile = File(...)):
        """
        Full voice round-trip:
          browser mic audio → STT → agent → TTS → WAV audio back to browser
        """
        import time
        t_total = time.perf_counter()

        raw = await audio.read()
        content_type = audio.content_type or "audio/webm"

        # STT
        transcript = ""
        t0 = time.perf_counter()
        try:
            transcript = await _stt.transcribe_bytes(raw, content_type)
        except Exception as e:
            logger.error(f"STT error: {e}")
            return JSONResponse({"error": f"STT failed: {e}"}, status_code=500)
        stt_ms = int((time.perf_counter() - t0) * 1000)

        if not transcript:
            return JSONResponse({"transcript": "", "text": "", "agent": _session.current_agent,
                                  "audio_b64": "", "handoff": None, "end_call": False,
                                  "timing": {"stt_ms": stt_ms, "llm_ms": 0, "tts_ms": 0, "total_ms": stt_ms}})

        # Language detection — update session so LLM responds in user's language
        _update_session_language(transcript)

        # Agent
        prev_agent = _session.current_agent
        t1 = time.perf_counter()
        try:
            response = await _orchestrator.process(transcript, _session)
        except Exception as e:
            logger.error(f"Agent error: {e}")
            return JSONResponse({"error": f"Agent failed: {e}"}, status_code=500)
        llm_ms = int((time.perf_counter() - t1) * 1000)

        handoff = {"from": prev_agent, "to": response.handoff.target} if response.handoff else None
        mcp = {k[4:]: v for k, v in _session.metadata.items() if k.startswith("mcp_")}
        meta = {k: v for k, v in _session.metadata.items() if not k.startswith("mcp_")}

        # TTS
        audio_b64 = ""
        tts_ms = 0
        if response.text:
            t2 = time.perf_counter()
            try:
                wav_bytes = await _tts.synthesize(response.text)
                audio_b64 = base64.b64encode(wav_bytes).decode()
            except Exception as e:
                logger.error(f"TTS error: {e}")
            tts_ms = int((time.perf_counter() - t2) * 1000)

        total_ms = int((time.perf_counter() - t_total) * 1000)

        return {
            "transcript": transcript,
            "text": response.text,
            "agent": _session.current_agent,
            "handoff": handoff,
            "end_call": response.end_call,
            "audio_b64": audio_b64,
            "metadata": meta,
            "mcp_results": mcp,
            "timing": {"stt_ms": stt_ms, "llm_ms": llm_ms, "tts_ms": tts_ms, "total_ms": total_ms},
        }

    @app.post("/voice_stream")
    async def voice_stream(audio: UploadFile = File(...)):
        """
        Low-latency voice pipeline:
          STT → LLM stream → sentence splitter → TTS stream → audio chunks to browser

        Key latency optimisation: LLM tokens are accumulated into sentences.
        The FIRST sentence is sent to TTS as soon as it ends (e.g. "Hello! How can I help?")
        so audio starts playing ~500ms after LLM starts, not after the full reply finishes.
        """
        import time, json as _json, re as _re
        raw = await audio.read()
        content_type = audio.content_type or "audio/webm"

        # Sentence boundary pattern — split on . ! ? followed by space or end
        _SENT = _re.compile(r'(?<=[.!?।])\s+')

        async def generate():
            t_total = time.perf_counter()

            # ── STT ───────────────────────────────────────────────────────────
            t0 = time.perf_counter()
            try:
                transcript = await _stt.transcribe_bytes(raw, content_type)
            except Exception as e:
                yield f"data: {_json.dumps({'type': 'error', 'error': str(e)})}\n\n"
                return
            stt_ms = int((time.perf_counter() - t0) * 1000)

            if not transcript:
                yield f"data: {_json.dumps({'type': 'empty'})}\n\n"
                return

            _update_session_language(transcript)

            # ── LLM — get full response (needed for handoff/end_call detection) ──
            prev_agent = _session.current_agent
            t1 = time.perf_counter()
            try:
                response = await _orchestrator.process(transcript, _session)
            except Exception as e:
                yield f"data: {_json.dumps({'type': 'error', 'error': str(e)})}\n\n"
                return
            llm_ms = int((time.perf_counter() - t1) * 1000)

            full_text = (response.text or "").strip()
            # Strip control tokens like [HANDOFF:x] [END_CALL] from spoken text
            spoken = _re.sub(r'\[(?:HANDOFF|END_CALL|MCP|TOOL):[^\]]*\]|\[END_CALL\]', '', full_text).strip()

            handoff  = {"from": prev_agent, "to": response.handoff.target} if response.handoff else None
            mcp      = {k[4:]: v for k, v in _session.metadata.items() if k.startswith("mcp_")}
            meta     = {k: v for k, v in _session.metadata.items() if not k.startswith("mcp_")}

            # Send meta immediately so UI updates (transcript + text visible) without waiting for TTS
            yield f"data: {_json.dumps({'type': 'meta', 'transcript': transcript, 'text': full_text, 'agent': _session.current_agent, 'handoff': handoff, 'end_call': response.end_call, 'metadata': meta, 'mcp_results': mcp, 'stt_ms': stt_ms, 'llm_ms': llm_ms})}\n\n"

            if response.end_call or not spoken:
                yield f"data: {_json.dumps({'type': 'done'})}\n\n"
                return

            # ── TTS — sentence-by-sentence streaming ─────────────────────────
            # Split reply into sentences and synthesize each one immediately.
            # First sentence audio arrives ~300-500ms after LLM finishes
            # instead of waiting for the entire paragraph to be synthesized.
            t2 = time.perf_counter()
            first_audio_sent = False
            sentences = [s.strip() for s in _SENT.split(spoken) if s.strip()] or [spoken]

            for sentence in sentences:
                if not sentence:
                    continue
                try:
                    async for chunk in _tts.stream_synthesize(sentence):
                        yield f"data: {_json.dumps({'type': 'audio', 'data': base64.b64encode(chunk).decode()})}\n\n"
                    # Tell browser this sentence is complete — play it immediately
                    yield f"data: {_json.dumps({'type': 'sentence_end'})}\n\n"
                except Exception as e:
                    logger.error(f"TTS error for sentence '{sentence[:40]}': {e}")

            tts_ms    = int((time.perf_counter() - t2) * 1000)
            total_ms  = int((time.perf_counter() - t_total) * 1000)
            yield f"data: {_json.dumps({'type': 'done', 'timing': {'stt_ms': stt_ms, 'llm_ms': llm_ms, 'tts_ms': tts_ms, 'total_ms': total_ms}})}\n\n"

        from fastapi.responses import StreamingResponse as _SR
        return _SR(generate(), media_type="text/event-stream",
                   headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/text")
    async def text_chat(body: dict):
        """Text-only path — same as voice but skips STT/TTS."""
        msg = (body.get("message") or "").strip()
        if not msg:
            return JSONResponse({"error": "empty"}, status_code=400)

        # Language detection — auto-detect from text input too
        _update_session_language(msg)

        prev_agent = _session.current_agent
        try:
            response = await _orchestrator.process(msg, _session)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

        handoff = {"from": prev_agent, "to": response.handoff.target} if response.handoff else None
        mcp = {k[4:]: v for k, v in _session.metadata.items() if k.startswith("mcp_")}
        meta = {k: v for k, v in _session.metadata.items() if not k.startswith("mcp_")}
        return {"text": response.text, "agent": _session.current_agent,
                "handoff": handoff, "end_call": response.end_call,
                "metadata": meta, "mcp_results": mcp}

    @app.post("/reset")
    async def reset(body: dict = {}):
        global _session
        _session = _orchestrator.start_session()
        # Seed English as default language on fresh session
        _session.set("language", "en")
        _session.set("language_instruction", "You MUST respond only in English.")
        if body.get("agent") and body["agent"] in _orchestrator._agents:
            _session.switch_agent(body["agent"])
        return {"current_agent": _session.current_agent}

    @app.post("/switch")
    async def switch(body: dict):
        name = body.get("agent", "")
        if name not in _orchestrator._agents:
            return JSONResponse({"error": f"Unknown agent: {name}"}, status_code=400)
        _session.switch_agent(name)
        return {"current_agent": _session.current_agent}

    @app.get("/welcome")
    async def welcome_audio():
        """Return the welcome message as MP3 audio for the studio to play on load."""
        from config.settings import Settings
        s = Settings.from_env()
        msg = s.welcome_message
        if not msg or not hasattr(_tts, "synthesize"):
            return JSONResponse({"audio_b64": "", "text": ""})
        try:
            mp3_bytes = await _tts.synthesize(msg)
            return {"audio_b64": base64.b64encode(mp3_bytes).decode(), "text": msg}
        except Exception as e:
            logger.error(f"Welcome TTS error: {e}")
            return JSONResponse({"audio_b64": "", "text": msg})

    return app


# ── Embedded UI ───────────────────────────────────────────────────────────────

_UI_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Studio</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:        #F7F6F3;
    --surface:   #FFFFFF;
    --border:    #E8E6E0;
    --text:      #1A1916;
    --muted:     #9A9690;
    --accent:    #2D5BE3;
    --accent-lt: #EEF2FD;
    --user-bg:   #1A1916;
    --user-text: #F7F6F3;
    --agent-bg:  #FFFFFF;
    --agent-text:#1A1916;
    --danger:    #D94F3D;
    --green:     #2EAF6E;
    --radius:    16px;
    --shadow:    0 1px 3px rgba(0,0,0,.06), 0 4px 16px rgba(0,0,0,.04);
  }

  body {
    font-family: 'DM Sans', sans-serif;
    background: var(--bg);
    color: var(--text);
    height: 100dvh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  /* ── Chat scroll container ── */
  #msgs-wrap {
    flex: 1;
    overflow-y: auto;
    scroll-behavior: smooth;
  }
  #msgs-wrap::-webkit-scrollbar { width: 0px; }

  /* ── Inner column — constrained width, centered ── */
  #msgs {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 24px 20px 16px;
    max-width: 680px;
    margin: 0 auto;
    min-height: 100%;
  }

  /* ── Messages ── */
  .msg {
    display: flex;
    flex-direction: column;
    max-width: min(460px, 75%);
    animation: fadeUp .22s ease both;
  }
  @keyframes fadeUp {
    from { opacity: 0; transform: translateY(8px); }
    to   { opacity: 1; transform: translateY(0); }
  }

  .msg.user  { align-self: flex-end; }
  .msg.agent { align-self: flex-start; }

  .bubble {
    padding: 11px 15px;
    border-radius: var(--radius);
    font-size: 14.5px;
    line-height: 1.55;
    font-weight: 400;
  }
  .msg.user  .bubble {
    background: var(--user-bg);
    color: var(--user-text);
    border-bottom-right-radius: 4px;
  }
  .msg.agent .bubble {
    background: var(--agent-bg);
    color: var(--agent-text);
    border: 1px solid var(--border);
    border-bottom-left-radius: 4px;
    box-shadow: var(--shadow);
  }

  /* ── Typing dots ── */
  .typing {
    display: flex;
    align-items: center;
    gap: 5px;
    padding: 13px 16px;
    background: var(--agent-bg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    border-bottom-left-radius: 4px;
    width: fit-content;
    box-shadow: var(--shadow);
  }
  .typing span {
    width: 5px; height: 5px;
    border-radius: 50%;
    background: var(--muted);
    animation: blink .9s ease-in-out infinite;
  }
  .typing span:nth-child(2) { animation-delay: .15s; }
  .typing span:nth-child(3) { animation-delay: .30s; }
  @keyframes blink {
    0%, 80%, 100% { transform: scale(1); opacity: .4; }
    40%           { transform: scale(1.25); opacity: 1; }
  }

  /* error */
  .err-bubble {
    background: #FEF2F0;
    border: 1px solid #F5C4BE;
    color: var(--danger);
    border-radius: var(--radius);
    padding: 10px 14px;
    font-size: 13px;
    align-self: flex-start;
  }

  /* system line */
  .sys-line {
    align-self: center;
    font-size: 11px;
    color: var(--muted);
    padding: 2px 10px;
    background: var(--border);
    border-radius: 20px;
    letter-spacing: .3px;
  }

  /* ── Bottom bar ── */
  #bottom {
    flex-shrink: 0;
    background: var(--surface);
    border-top: 1px solid var(--border);
    padding: 12px 20px 14px;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  /* inner wrapper */
  #bottom-inner {
    display: flex;
    flex-direction: row;
    align-items: center;
    justify-content: center;
    gap: 16px;
    width: 100%;
    max-width: 700px;
  }

  /* waveform row */
  #wave-row {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 3px;
    height: 22px;
    min-width: 80px;
  }
  .bar {
    width: 3px;
    border-radius: 2px;
    background: var(--border);
    transition: height .05s, background .15s;
  }

  /* mic button */
  #mic-wrap {
    display: flex;
    flex-direction: row;
    align-items: center;
    gap: 8px;
  }
  #mic-btn {
    width: 40px; height: 40px;
    border-radius: 50%;
    border: none;
    background: var(--user-bg);
    color: #fff;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: transform .15s, box-shadow .15s, background .2s;
    box-shadow: 0 2px 8px rgba(0,0,0,.18);
    position: relative;
    padding: 0;
    overflow: hidden;
    flex-shrink: 0;
  }
  #mic-btn img {
    width: 26px;
    height: 26px;
    object-fit: contain;
    filter: brightness(0) invert(1);
    opacity: 0.9;
  }
  #mic-btn:hover { transform: scale(1.06); }

  #mic-btn.listening {
    background: var(--user-bg);
    box-shadow: 0 0 0 0 rgba(26,25,22,.25);
    animation: mic-pulse 2.4s ease-in-out infinite;
  }
  #mic-btn.speaking {
    background: var(--danger);
    animation: none;
    box-shadow: 0 0 0 4px rgba(217,79,61,.2);
  }
  #mic-btn.processing {
    background: var(--muted);
    cursor: not-allowed;
    animation: none;
  }
  #mic-btn.playing {
    background: var(--accent);
    animation: none;
    box-shadow: 0 0 0 4px rgba(45,91,227,.15);
  }
  @keyframes mic-pulse {
    0%,100% { box-shadow: 0 0 0 0 rgba(26,25,22,.2); }
    50%      { box-shadow: 0 0 0 10px rgba(26,25,22,0); }
  }

  #mic-label {
    font-size: 11px;
    color: var(--muted);
    letter-spacing: .3px;
    font-weight: 500;
    text-transform: uppercase;
    white-space: nowrap;
  }

  /* recalibrate tiny link */
  #recal-btn {
    font-size: 10px;
    color: var(--muted);
    background: none;
    border: none;
    cursor: pointer;
    letter-spacing: .2px;
    padding: 2px 5px;
    border-radius: 4px;
    transition: color .15s;
    white-space: nowrap;
  }
  #recal-btn:hover { color: var(--text); }

  /* welcome screen */
  .welcome {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 8px;
    color: var(--muted);
    text-align: center;
    padding-bottom: 32px;
  }
  .welcome-ring {
    width: 64px; height: 64px;
    border-radius: 50%;
    border: 2px solid var(--border);
    display: flex; align-items: center; justify-content: center;
    font-size: 26px;
    margin-bottom: 8px;
    background: var(--surface);
    box-shadow: var(--shadow);
  }
  .welcome p { font-size: 15px; font-weight: 500; color: var(--text); }
  .welcome small { font-size: 12px; color: var(--muted); line-height: 1.6; }
</style>
</head>
<body>

<div id="msgs-wrap">
<div id="msgs">
  <div class="welcome" id="welcome-screen">
    <div class="welcome-ring">🎙</div>
    <p>Ready to listen</p>
    <small>Just start speaking — the agent will respond.<br>Tap the mic below to mute.</small>
  </div>
</div>
</div>

<div id="bottom">
  <div id="bottom-inner">
  <!-- live audio waveform bars -->
  <div id="wave-row">
    <div class="bar" id="b0"></div>
    <div class="bar" id="b1"></div>
    <div class="bar" id="b2"></div>
    <div class="bar" id="b3"></div>
    <div class="bar" id="b4"></div>
    <div class="bar" id="b5"></div>
    <div class="bar" id="b6"></div>
    <div class="bar" id="b7"></div>
    <div class="bar" id="b8"></div>
    <div class="bar" id="b9"></div>
    <div class="bar" id="b10"></div>
    <div class="bar" id="b11"></div>
  </div>

  <div id="mic-wrap">
    <button id="mic-btn" title="Click to mute / unmute">
      <img src="data:image/png;base64,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" alt="CRM Landing" />
    </button>
    <span id="mic-label">connecting…</span>
    <button id="recal-btn" onclick="recalibrateNow()">recalibrate</button>
  </div>
  </div>
</div>

<script>
// ── State machine ──────────────────────────────────────────────────────────
const S = {IDLE:'idle', LISTENING:'listening', SPEAKING:'speaking',
           THINKING:'thinking', PROCESSING:'processing', PLAYING:'playing'};
let appState = S.IDLE;
let currentAgent = '';
let muted = false;
let pendingUserDiv = null;

// ── Audio / VAD ────────────────────────────────────────────────────────────
let audioCtx = null;
let analyser = null;
let micStream = null;
let mediaRecorder = null;
let audioChunks = [];
let silenceTimer = null;
let speechStartTime = null;
let vadRaf = null;

let   NOISE_FLOOR    = 0.010;
let   SPEECH_THRESH  = 0.035;
let   SILENCE_THRESH = 0.013;
const SILENCE_MS     = 900;   // wait longer after silence before submitting
const MIN_SPEECH_MS  = 600;   // must speak for at least 600ms — filters noise bursts
const ONSET_FRAMES   = 6;     // need 6 consecutive frames above threshold (was 4)
let   speechOnsetTimer = null;
let   onsetCount       = 0;
let   bargeCount       = 0;

// ── Single always-on recorder ─────────────────────────────────────────────


// ── Waveform bars ──────────────────────────────────────────────────────────
const BARS = 12;
const barEls = Array.from({length: BARS}, (_, i) => document.getElementById('b' + i));

function updateWaveBars(rms) {
  // Smoothed visual level
  const lvl = Math.min(1, rms / 0.08);
  barEls.forEach((el, i) => {
    const center = (BARS - 1) / 2;
    const dist   = Math.abs(i - center) / center;         // 0 at center, 1 at edges
    const shape  = Math.max(0, 1 - dist * 0.6);           // bell-ish curve
    const noise  = 0.15 + Math.random() * 0.25;           // organic jitter
    const h      = appState === S.SPEAKING || appState === S.PLAYING
                   ? Math.round(4 + (lvl * shape + noise * lvl) * 20)
                   : Math.round(3 + noise * 3);
    el.style.height = h + 'px';

    const isActive = appState === S.SPEAKING;
    const isPlay   = appState === S.PLAYING;
    el.style.background = isActive ? 'var(--danger)'
                        : isPlay   ? 'var(--accent)'
                        : 'var(--border)';
  });
}

// ── Init ───────────────────────────────────────────────────────────────────
async function init() {
  const d = await fetch('/agents').then(r => r.json());
  currentAgent = d.current;

  document.getElementById('mic-btn').addEventListener('click', () => {
    if (appState === S.IDLE) return;
    muted = !muted;
    if (muted) {
      abortRecording();
      setAppState(S.LISTENING);
      document.getElementById('mic-label').textContent = 'muted';
      document.getElementById('mic-btn').style.opacity = '0.35';
    } else {
      document.getElementById('mic-btn').style.opacity = '1';
      setAppState(S.LISTENING);
    }
  });

  await startMic();
}

async function startMic() {
  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl:  false,
        channelCount:     1,
        sampleRate:       16000,
      }
    });
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 1024;
    analyser.smoothingTimeConstant = 0.2;
    audioCtx.createMediaStreamSource(micStream).connect(analyser);

    document.getElementById('mic-label').textContent = 'calibrating…';
    await calibrateNoise();

    setAppState(S.LISTENING);
    runVAD();
    playWelcome();
  } catch(e) {
    appendError('Mic access denied — ' + e.message);
  }
}

async function playWelcome() {
  try {
    const d = await fetch('/welcome').then(r => r.json());
    if (!d.audio_b64) return;
    if (d.text) appendMsg('agent', d.text, null, false);
    const binary = atob(d.audio_b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    const decoded = await audioCtx.decodeAudioData(bytes.buffer);
    audioSource = audioCtx.createBufferSource();
    audioSource.buffer = decoded;
    audioSource.connect(audioCtx.destination);
    audioSource.onended = () => {
      audioSource = null;
      if (appState === S.PLAYING) { resetPlayback(); setAppState(S.LISTENING); }
    };
    audioSource.start(0);
    playbackStarted = true;
    setAppState(S.PLAYING);
  } catch(e) { console.warn('Welcome audio error:', e.message); }
}

async function calibrateNoise(silent = false) {
  const buf = new Float32Array(analyser.fftSize);
  const samples = [];
  const t0 = Date.now();
  await new Promise(resolve => {
    (function tick() {
      analyser.getFloatTimeDomainData(buf);
      let s = 0; for (let i = 0; i < buf.length; i++) s += buf[i]*buf[i];
      samples.push(Math.sqrt(s / buf.length));
      if (Date.now() - t0 < 2000) requestAnimationFrame(tick); else resolve();
    })();
  });
  const sorted = [...samples].sort((a,b) => a-b);
  NOISE_FLOOR    = sorted[Math.floor(sorted.length * 0.95)];  // 95th percentile — more conservative
  SPEECH_THRESH  = Math.min(0.35, Math.max(0.030, NOISE_FLOOR * 5.0));  // 5x noise (was 3.5x)
  SILENCE_THRESH = Math.min(SPEECH_THRESH * 0.50, Math.max(0.012, NOISE_FLOOR * 1.5));  // tighter gap
  if (!silent) {
    console.log('noise=' + NOISE_FLOOR.toFixed(4) + ' start=' + SPEECH_THRESH.toFixed(4) + ' stop=' + SILENCE_THRESH.toFixed(4));
    document.getElementById('mic-label').textContent = 'ready';
    setTimeout(() => {
      if (appState === S.LISTENING) document.getElementById('mic-label').textContent = 'listening…';
    }, 1500);
  }
}
setInterval(() => { if (appState === S.LISTENING && !muted) calibrateNoise(true); }, 45000);

async function recalibrateNow() {
  document.getElementById('mic-label').textContent = 'calibrating…';
  await calibrateNoise(false);
}

// ── VAD loop ───────────────────────────────────────────────────────────────
function runVAD() {
  const buf = new Float32Array(analyser.fftSize);

  function tick() {
    vadRaf = requestAnimationFrame(tick);
    analyser.getFloatTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
    const rms = Math.sqrt(sum / buf.length);

    updateWaveBars(rms);

    if (muted) return;

    // ── BARGE-IN: user speaks while agent is talking or processing ──────────
    if (appState === S.PLAYING || appState === S.PROCESSING) {
      if (rms > SPEECH_THRESH) {
        bargeCount++;
        if (bargeCount >= 5) {
          bargeCount = 0;
          stopPlayback();
          if (activeAbort) { activeAbort.abort(); activeAbort = null; }
          onsetCount = 0;
          speechStartTime = Date.now();
          beginRecording();
          setAppState(S.SPEAKING);
        }
      } else { bargeCount = 0; }
      return;
    }

    if (appState === S.LISTENING) {
      if (rms > SPEECH_THRESH) {
        onsetCount++;
        if (onsetCount >= ONSET_FRAMES && !speechOnsetTimer) {
          speechOnsetTimer = setTimeout(() => {
            speechOnsetTimer = null;
            speechStartTime  = Date.now();
            onsetCount       = 0;
            beginRecording();
            setAppState(S.SPEAKING);
          }, 0);
        }
      } else {
        onsetCount = 0;
        if (speechOnsetTimer) { clearTimeout(speechOnsetTimer); speechOnsetTimer = null; }
      }
    } else if (appState === S.THINKING) {
      if (rms > SPEECH_THRESH) {
        onsetCount++;
        if (onsetCount >= 3) {
          cancelThinkTimer();
          onsetCount = 0;
          if (pendingUserDiv) { pendingUserDiv.remove(); pendingUserDiv = null; }
          setAppState(S.SPEAKING);
          speechStartTime = Date.now();
        }
      } else { onsetCount = 0; }
    } else if (appState === S.SPEAKING) {
      if (rms < SILENCE_THRESH) {
        if (!silenceTimer) {
          silenceTimer = setTimeout(() => {
            silenceTimer = null;
            if (appState !== S.SPEAKING) return;
            const dur = Date.now() - speechStartTime;
            if (dur >= MIN_SPEECH_MS) { finishRecording(); }
            else { abortRecording(); setAppState(S.LISTENING); }
          }, SILENCE_MS);
        }
      } else { clearSilenceTimer(); }
    }
  }
  tick();
}

function clearSilenceTimer() {
  if (silenceTimer) { clearTimeout(silenceTimer); silenceTimer = null; }
}

const MAX_RECORD_MS = 20000;
let maxRecordTimer = null;

function beginRecording() {
  audioChunks = [];
  // Use simplest possible mimeType — server converts to WAV via ffmpeg anyway
  const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
    ? 'audio/webm;codecs=opus'
    : MediaRecorder.isTypeSupported('audio/webm')
    ? 'audio/webm'
    : '';
  const opts = mimeType ? {mimeType, audioBitsPerSecond: 128000} : {};
  mediaRecorder = new MediaRecorder(micStream, opts);
  mediaRecorder.ondataavailable = e => { if (e.data && e.data.size > 0) audioChunks.push(e.data); };
  mediaRecorder.start(100);
  maxRecordTimer = setTimeout(() => {
    if (appState === S.SPEAKING) finishRecording();
  }, MAX_RECORD_MS);
}

function abortRecording() {
  clearSilenceTimer();
  cancelThinkTimer();
  if (maxRecordTimer) { clearTimeout(maxRecordTimer); maxRecordTimer = null; }
  if (speechOnsetTimer) { clearTimeout(speechOnsetTimer); speechOnsetTimer = null; }
  if (pendingUserDiv) { pendingUserDiv.remove(); pendingUserDiv = null; }
  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    mediaRecorder.onstop = null;
    mediaRecorder.stop();
  }
  mediaRecorder = null;
  audioChunks = [];
}

const THINK_MS = 0;
let thinkTimer = null;

function cancelThinkTimer() {
  if (thinkTimer) { clearTimeout(thinkTimer); thinkTimer = null; }
}

function finishRecording() {
  cancelThinkTimer();
  clearSilenceTimer();
  if (maxRecordTimer) { clearTimeout(maxRecordTimer); maxRecordTimer = null; }
  setAppState(S.THINKING);

  if (!pendingUserDiv) {
    pendingUserDiv = appendMsg('user', '…', null, false);
  }

  thinkTimer = setTimeout(() => {
    thinkTimer = null;
    if (appState !== S.THINKING) return;
    if (!mediaRecorder || mediaRecorder.state === 'inactive') {
      setAppState(S.LISTENING);
      return;
    }
    setAppState(S.PROCESSING);

    // Capture mimeType BEFORE stopping — it becomes empty string after stop
    const mimeType = mediaRecorder.mimeType || 'audio/webm;codecs=opus';

    mediaRecorder.onstop = async () => {
      // Wait 150ms so Chrome flushes the very last chunk into audioChunks
      await new Promise(r => setTimeout(r, 150));

      // Need at least 4 chunks for a valid WebM (header + data)
      if (audioChunks.length < 4) {
        if (pendingUserDiv) { pendingUserDiv.remove(); pendingUserDiv = null; }
        audioChunks = [];
        mediaRecorder = null;
        setAppState(S.LISTENING);
        return;
      }

      const blob = new Blob(audioChunks, {type: mimeType});
      audioChunks = [];
      mediaRecorder = null;

      if (blob.size > 1800000) {
        if (pendingUserDiv) { pendingUserDiv.remove(); pendingUserDiv = null; }
        appendError('Recording too long — please keep under 20 seconds.');
        setAppState(S.LISTENING);
        return;
      }
      if (blob.size < 500) {
        if (pendingUserDiv) { pendingUserDiv.remove(); pendingUserDiv = null; }
        setAppState(S.LISTENING);
        return;
      }
      await sendVoice(blob);
    };

    // Request final data flush before stopping
    mediaRecorder.requestData();
    mediaRecorder.stop();
  }, THINK_MS);
}

// ── Streaming TTS playback ─────────────────────────────────────────────────
let mp3Chunks       = [];
let activeAbort     = null;
let audioQueue      = [];
let audioSource     = null;
let playbackStarted = false;

function collectMp3Chunk(b64) {
  const binary = atob(b64);
  const chunk  = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) chunk[i] = binary.charCodeAt(i);
  mp3Chunks.push(chunk);
}

async function flushSentenceChunks() {
  if (mp3Chunks.length === 0) return;
  const total  = mp3Chunks.reduce((s, c) => s + c.length, 0);
  const merged = new Uint8Array(total);
  let   offset = 0;
  for (const c of mp3Chunks) { merged.set(c, offset); offset += c.length; }
  mp3Chunks = [];
  try {
    const decoded = await audioCtx.decodeAudioData(merged.buffer.slice(0));
    audioQueue.push(decoded);
    if (!playbackStarted) playNextInQueue();
  } catch(e) { console.error('Audio decode error:', e.message); }
}

function playNextInQueue() {
  if (audioQueue.length === 0) {
    audioSource     = null;
    playbackStarted = false;
    if (appState === S.PLAYING) { resetPlayback(); setAppState(S.LISTENING); }
    return;
  }
  const buf   = audioQueue.shift();
  audioSource = audioCtx.createBufferSource();
  audioSource.buffer = buf;
  audioSource.connect(audioCtx.destination);
  audioSource.onended = () => playNextInQueue();
  audioSource.start(0);
  playbackStarted = true;
  setAppState(S.PLAYING);
}

async function playAllMp3Chunks() {
  await flushSentenceChunks();
  return 0;
}

function stopPlayback() {
  if (audioSource) { try { audioSource.stop(); } catch(e) {} audioSource = null; }
  audioQueue = [];
  resetPlayback();
}

function resetPlayback() {
  playbackStarted = false;
  mp3Chunks       = [];
  audioQueue      = [];
  bargeCount      = 0;
}

// ── Send voice (streaming SSE) ─────────────────────────────────────────────
async function sendVoice(blob) {
  const form = new FormData();
  form.append('audio', blob, 'recording.webm');

  activeAbort  = new AbortController();
  const signal = activeAbort.signal;

  let resp;
  try {
    resp = await fetch('/voice_stream', {method: 'POST', body: form, signal});
  } catch(e) {
    activeAbort = null;
    if (e.name === 'AbortError') return;
    if (pendingUserDiv) { pendingUserDiv.remove(); pendingUserDiv = null; }
    appendError('Voice error: ' + e.message);
    setAppState(S.LISTENING);
    return;
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  let lastAgentDiv = null;
  resetPlayback();

  try {
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      if (signal.aborted) break;
      buf += decoder.decode(value, {stream: true});

      const parts = buf.split('\n\n');
      buf = parts.pop();

      for (const part of parts) {
        const line = part.trim();
        if (!line.startsWith('data: ')) continue;
        let ev;
        try { ev = JSON.parse(line.slice(6)); } catch { continue; }

        if (ev.type === 'error') {
          if (pendingUserDiv) { pendingUserDiv.remove(); pendingUserDiv = null; }
          appendError(ev.error);
          setAppState(S.LISTENING);
          return;
        }

        if (ev.type === 'empty') {
          if (pendingUserDiv) { pendingUserDiv.remove(); pendingUserDiv = null; }
          setAppState(S.LISTENING);
          return;
        }

        if (ev.type === 'meta') {
          // update pending user bubble with real transcript
          if (pendingUserDiv) {
            pendingUserDiv.querySelector('.bubble').textContent = ev.transcript || '—';
            pendingUserDiv = null;
          } else {
            appendMsg('user', ev.transcript, null, false);
          }
          currentAgent = ev.agent;
          lastAgentDiv = appendMsg('agent', ev.text, null, ev.end_call);
          if (ev.end_call) { appendSysLine('Call ended'); setAppState(S.IDLE); return; }
          setAppState(S.PROCESSING);
        }

        if (ev.type === 'audio' && ev.data) collectMp3Chunk(ev.data);

        if (ev.type === 'sentence_end') flushSentenceChunks();

        if (ev.type === 'done') { /* timing available but not shown */ }
      }
    }
  } catch(e) {
    if (!signal.aborted) appendError('Stream error: ' + e.message);
  }

  activeAbort = null;
  await flushSentenceChunks();
  if (!playbackStarted) setAppState(S.LISTENING);
}

// ── UI helpers ─────────────────────────────────────────────────────────────
function setAppState(s) {
  appState = s;
  const btn = document.getElementById('mic-btn');
  const lbl = document.getElementById('mic-label');
  btn.className = '';
  if (muted) return;
  const map = {
    [S.IDLE]:       ['', 'idle'],
    [S.LISTENING]:  ['listening', 'listening…'],
    [S.SPEAKING]:   ['speaking',  'speaking…'],
    [S.THINKING]:   ['speaking',  'processing…'],
    [S.PROCESSING]: ['processing','processing…'],
    [S.PLAYING]:    ['playing',   'speaking — interrupt anytime'],
  };
  const [cls, txt] = map[s] || ['', ''];
  if (cls) btn.classList.add(cls);
  lbl.textContent = txt;
}

function removeWelcome() {
  const w = document.getElementById('welcome-screen');
  if (w) w.remove();
}

function appendMsg(role, text, _ignored, endCall) {
  removeWelcome();
  const msgs = document.getElementById('msgs');
  const div = document.createElement('div');
  div.className = 'msg ' + role;

  const bub = document.createElement('div');
  bub.className = 'bubble';
  bub.textContent = text || '—';
  div.appendChild(bub);

  if (endCall) {
    const el = document.createElement('div');
    el.className = 'sys-line';
    el.style.marginTop = '6px';
    el.textContent = 'call ended';
    div.appendChild(el);
  }

  msgs.appendChild(div);
  document.getElementById('msgs-wrap').scrollTop = 99999;
  return div;
}

function appendSysLine(text) {
  removeWelcome();
  const msgs = document.getElementById('msgs');
  const div = document.createElement('div');
  div.className = 'sys-line';
  div.textContent = text;
  msgs.appendChild(div);
  document.getElementById('msgs-wrap').scrollTop = 99999;
}

function appendError(msg) {
  removeWelcome();
  const msgs = document.getElementById('msgs');
  const div = document.createElement('div');
  div.className = 'err-bubble';
  div.textContent = msg;
  msgs.appendChild(div);
  document.getElementById('msgs-wrap').scrollTop = 99999;
}

async function post(url, body) {
  const r = await fetch(url, {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  return r.json();
}

// keep background state synced (no UI shown for it)
async function refreshState() {
  try { await fetch('/state'); } catch(e) {}
}

init();
</script>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="Agent Studio — voice + text browser UI")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--mock", action="store_true", help="Stub all providers, no API keys needed")
    parser.add_argument("--no-rag", action="store_true")
    parser.add_argument("--no-mcp", action="store_true")
    args = parser.parse_args()

    app = create_app(mock=args.mock, no_rag=args.no_rag, no_mcp=args.no_mcp)
    print(f"\n  Agent Studio → http://{args.host}:{args.port}\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="debug")


if __name__ == "__main__":
    main()