"""
Asterisk AudioSocket — TCP server handler.

AudioSocket is a binary protocol over TCP. Asterisk sends/receives raw slin16
PCM audio frames (signed 16-bit LE, 8 kHz, mono).

Frame format:
    [type: 1 byte][length: 2 bytes big-endian][payload: N bytes]

Type values:
    0x00  UUID    — 16-byte UUID identifying the call (sent once at connection start)
    0x10  SLIN    — Audio data (slin16 PCM)
    0xFF  HANGUP  — Call ended

Usage:
    In Asterisk dialplan:
        exten => _X.,1,Answer()
         same => n,Wait(0.2)
         same => n,AudioSocket(${UNIQUEID},127.0.0.1:9093)
         same => n,Hangup()
"""
from __future__ import annotations

import asyncio
import logging
import uuid

logger = logging.getLogger(__name__)

# AudioSocket frame types
AS_TYPE_UUID = 0x00
AS_TYPE_SLIN = 0x10
AS_TYPE_HANGUP = 0xFF


async def start_audiosocket_server(pipeline, host: str = "0.0.0.0", port: int = 9093):
    """Start the AudioSocket TCP server."""

    async def handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        logger.info(f"AudioSocket connection from {peer}")
        
        # Read first frame — should be UUID
        call_sid = str(uuid.uuid4())  # fallback
        try:
            header = await asyncio.wait_for(reader.readexactly(3), timeout=5.0)
            frame_type = header[0]
            length = int.from_bytes(header[1:3], "big")
            if length > 0:
                payload = await asyncio.wait_for(reader.readexactly(length), timeout=5.0)
                print(f"payload received: {payload}")
            else:
                payload = b""

            if frame_type == AS_TYPE_UUID and len(payload) == 16:
                call_sid = str(uuid.UUID(bytes=payload))
                logger.info(f"AudioSocket UUID: {call_sid}")
                
            elif frame_type == AS_TYPE_UUID:
                # Some Asterisk versions send UUID as ASCII string
                call_sid = payload.decode("utf-8", errors="replace").strip("\x00")
                logger.info(f"AudioSocket UUID (string): {call_sid}")
        except Exception as e:
            logger.warning(f"AudioSocket: failed to read UUID frame: {e}")

        try:
            await pipeline.run_audiosocket_call(call_sid, reader, writer)
        except Exception:
            logger.exception(f"AudioSocket: error during call {call_sid}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            logger.info(f"AudioSocket: call ended {call_sid}")

    server = await asyncio.start_server(handle_connection, host, port)
    addr = server.sockets[0].getsockname() if server.sockets else (host, port)
    logger.info(f"AudioSocket TCP server listening on {addr[0]}:{addr[1]}")
    return server
