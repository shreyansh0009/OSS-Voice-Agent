"""
Entry point. Builds the LLM/STT/TTS providers, wires the pipeline,
and starts the AudioSocket TCP server + FastAPI HTTP admin server.
"""
import asyncio
import logging

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI

from config.settings import Settings
from core.streaming_pipeline import StreamingPipeline
from core.latency_logger import get_latency_logger
from agents.registry import build_orchestrator
from api.audiosocket import start_audiosocket_server
from api.admin import router as admin_router

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


def build_llm(settings: Settings):
    if settings.llm_provider == "groq":
        from providers.llm.groq_provider import GroqLLM
        return GroqLLM(
            api_key=settings.groq_api_key,
            model=settings.groq_model,
            temperature=settings.llm_temperature,
        )
    elif settings.llm_provider == "openai":
        from providers.llm.openai_provider import OpenAILLM
        return OpenAILLM(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            temperature=settings.llm_temperature,
        )
    elif settings.llm_provider == "litellm":
        from providers.llm.litellm_provider import LiteLLMProvider
        return LiteLLMProvider.from_env()
    else:
        from providers.llm.ollama import OllamaLLM
        return OllamaLLM(
            base_url=settings.ollama_base_url,
            model=settings.ollama_model,
            temperature=settings.llm_temperature,
        )


def build_stt(settings: Settings):
    if settings.stt_provider == "deepgram":
        from providers.stt.deepgram import DeepgramSTT
        return DeepgramSTT(
            api_key=settings.deepgram_api_key,
            model=settings.deepgram_model,
            language=settings.deepgram_language,
            endpointing_ms=settings.deepgram_endpointing_ms,
        )
    else:
        from providers.stt.whisper import WhisperSTT
        return WhisperSTT(
            model_size=settings.whisper_model,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute,
        )


def build_tts(settings: Settings):
    if settings.tts_provider == "elevenlabs":
        from providers.tts.elevenlabs import ElevenLabsTTS
        voice_language_map = {
            lang: voice_id
            for lang, voice_id in {
                "hi": settings.elevenlabs_voice_hi,
                "bn": settings.elevenlabs_voice_bn,
                "te": settings.elevenlabs_voice_te,
                "mr": settings.elevenlabs_voice_mr,
                "ta": settings.elevenlabs_voice_ta,
                "gu": settings.elevenlabs_voice_gu,
                "kn": settings.elevenlabs_voice_kn,
                "pa": settings.elevenlabs_voice_pa,
                "ml": settings.elevenlabs_voice_ml,
                "or": settings.elevenlabs_voice_or,
            }.items()
            if voice_id
        }
        return ElevenLabsTTS(
            api_key=settings.elevenlabs_api_key,
            voice_id=settings.elevenlabs_voice_id,
            model=settings.elevenlabs_model,
            stability=settings.elevenlabs_stability,
            similarity_boost=settings.elevenlabs_similarity_boost,
            style=settings.elevenlabs_style,
            voice_language_map=voice_language_map,
        )
    elif settings.tts_provider == "cartesia":
        from providers.tts.cartesia import CartesiaTTS
        return CartesiaTTS(
            api_key=settings.cartesia_api_key,
            voice_id=settings.cartesia_voice_id,
            model=settings.cartesia_model,
            number_speed=settings.cartesia_number_speed,
        )
    elif settings.tts_provider == "kokoro":
        from providers.tts.kokoro import KokoroTTS
        return KokoroTTS(model_path=settings.kokoro_model_path, voice=settings.kokoro_voice)
    else:
        from providers.tts.piper import PiperTTS
        return PiperTTS(model_path=settings.piper_model_path)


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="Voice Agent — AudioSocket", version="2.0.0")

    app.include_router(admin_router)  # /admin/health, etc.

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "llm": settings.llm_provider,
            "stt": settings.stt_provider,
            "tts": settings.tts_provider,
            "audiosocket_port": settings.audiosocket_port,
        }

    @app.get("/api/asterisk/register-call")
    def register_call(uuid: str = "", did: str = ""):
        """Called by Asterisk dialplan before AudioSocket to register the call."""
        logger.info(f"Asterisk register-call: uuid={uuid} did={did}")
        return {"status": "ok", "uuid": uuid, "did": did}

    logger.info("FastAPI app ready (HTTP admin)")
    return app


async def main():
    settings = Settings.from_env()

    llm = build_llm(settings)
    stt = build_stt(settings)
    tts = build_tts(settings)

    logger.info(f"LLM={settings.llm_provider} STT={settings.stt_provider} TTS={settings.tts_provider}")

    orchestrator = build_orchestrator(settings.squad_path, llm)
    pipeline = StreamingPipeline(
        stt=stt,
        tts=tts,
        llm=llm,
        orchestrator=orchestrator,
        welcome_message=settings.welcome_message,
    )

    # Start latency logger (background writer — zero-latency file appender)
    lat = get_latency_logger()
    await lat.start()
    logger.info(f"LatencyLogger appending to latency.log")

    # Start AudioSocket TCP server
    tcp_server = await start_audiosocket_server(
        pipeline,
        host=settings.audiosocket_host,
        port=settings.audiosocket_port,
    )

    # Start FastAPI HTTP server (admin endpoints)
    app = create_app(settings)
    config = uvicorn.Config(app, host=settings.host, port=settings.port, log_level="info")
    http_server = uvicorn.Server(config)

    logger.info(
        f"Servers running:\n"
        f"  AudioSocket TCP → {settings.audiosocket_host}:{settings.audiosocket_port}\n"
        f"  HTTP Admin      → {settings.host}:{settings.port}"
    )

    try:
        await http_server.serve()
    finally:
        tcp_server.close()
        await tcp_server.wait_closed()
        await lat.stop()
        logger.info("LatencyLogger stopped")


if __name__ == "__main__":
    asyncio.run(main())
