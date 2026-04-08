import asyncio, json, os
import numpy as np
import sounddevice as sd
from dotenv import load_dotenv
load_dotenv()

KEY = os.getenv("DEEPGRAM_API_KEY", "").strip()

URL = (
    "wss://api.deepgram.com/v1/listen"
    "?model=nova-3"
    "&encoding=linear16"
    "&sample_rate=16000"
    "&channels=1"
    "&endpointing=100"
    "&interim_results=true"
    "&smart_format=true"
    "&no_delay=true"
    "&vad_events=true"
)

async def run():
    import websockets
    print("\n🎤 Speak 'hello' or 'yes' — listening for 15 seconds...\n")
    async with websockets.connect(URL, additional_headers={"Authorization": f"Token {KEY}"}) as ws:
        loop = asyncio.get_event_loop()
        def cb(indata, frames, t, status):
            pcm = (indata[:, 0] * 32767).astype(np.int16).tobytes()
            asyncio.run_coroutine_threadsafe(ws.send(pcm), loop)
        async def recv():
            async for raw in ws:
                msg = json.loads(raw)
                typ = msg.get("type", "")
                if typ == "SpeechStarted":
                    print("  [SpeechStarted] 🎙️")
                elif typ == "Results":
                    alts = msg.get("channel", {}).get("alternatives", [])
                    text = alts[0].get("transcript", "").strip() if alts else ""
                    is_f = msg.get("is_final", False)
                    sp_f = msg.get("speech_final", False)
                    if text:
                        tag = "PATH A" if (is_f and sp_f) else ("PATH C pending" if is_f else "interim")
                        print(f"  [Results] {tag} | '{text}' | is_final={is_f} speech_final={sp_f}")
                elif typ == "UtteranceEnd":
                    print("  [UtteranceEnd] unexpected — should not appear")
        with sd.InputStream(samplerate=16000, channels=1, dtype="float32", blocksize=4000, callback=cb):
            try:
                await asyncio.wait_for(recv(), timeout=15)
            except asyncio.TimeoutError:
                print("\nDone. Share the output above.")

asyncio.run(run())
