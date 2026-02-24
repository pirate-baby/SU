"""
ElevenLabs TTS WebSocket streaming client.

Opens a WebSocket to ElevenLabs TTS for one assistant turn.
Text chunks are fed in as they arrive from Claude;
audio chunks are yielded back as base64-encoded MP3 fragments.
"""
import asyncio
import json
from typing import AsyncGenerator, Optional

import websockets

from app.config import settings
from app.logger import get_logger

log = get_logger(__name__)

TTS_WS_URL = "wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input"


class ElevenLabsTTS:
    """Manages a single TTS WebSocket session for one assistant turn."""

    def __init__(self, voice_id: Optional[str] = None, model_id: Optional[str] = None):
        self.voice_id = voice_id or settings.elevenlabs_voice_id
        if not self.voice_id:
            raise ValueError("ELEVENLABS_VOICE_ID must be configured for voice mode")
        self.model_id = model_id or settings.elevenlabs_tts_model
        self._ws = None
        self._audio_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
        self._receive_task: Optional[asyncio.Task] = None
        self._chunks_received = 0
        self._text_chunks_sent = 0

    async def connect(self):
        """Open TTS WebSocket and send BOS (beginning of stream) message."""
        url = TTS_WS_URL.format(voice_id=self.voice_id)
        url += f"?model_id={self.model_id}&output_format=mp3_44100_128"

        extra_headers = {"xi-api-key": settings.elevenlabs_api_key}
        self._ws = await websockets.connect(url, additional_headers=extra_headers)

        # Send BOS — a blank space with voice settings and generation config
        bos_message = {
            "text": " ",
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
                "speed": 1.0,
            },
            "generation_config": {
                "chunk_length_schedule": [50, 90, 120, 150],
            },
        }
        await self._ws.send(json.dumps(bos_message))
        log.info("tts.connected", voice_id=self.voice_id, model_id=self.model_id)

        # Background task to receive audio chunks from TTS
        self._receive_task = asyncio.create_task(self._receive_audio())

    async def send_text(self, text: str):
        """Send a text chunk to TTS."""
        if not self._ws:
            return
        msg = {
            "text": text,
            "try_trigger_generation": True,
        }
        await self._ws.send(json.dumps(msg))
        self._text_chunks_sent += 1

    async def flush(self):
        """Force generation of any remaining buffered text."""
        if not self._ws:
            return
        await self._ws.send(json.dumps({"text": " ", "flush": True}))
        log.debug("tts.flushed", text_chunks_sent=self._text_chunks_sent)

    async def close(self):
        """Send EOS and close the WebSocket."""
        if not self._ws:
            return
        try:
            # Send EOS (empty text)
            await self._ws.send(json.dumps({"text": ""}))
        except Exception:
            log.debug("tts.eos_send_failed")
        # Wait for receive task to finish
        if self._receive_task:
            try:
                await asyncio.wait_for(self._receive_task, timeout=10.0)
            except asyncio.TimeoutError:
                log.warning("tts.receive_timeout", audio_chunks=self._chunks_received)
                self._receive_task.cancel()
        try:
            await self._ws.close()
        except Exception:
            pass
        log.info(
            "tts.closed",
            text_chunks_sent=self._text_chunks_sent,
            audio_chunks_received=self._chunks_received,
        )
        self._ws = None

    async def _receive_audio(self):
        """Background task: read audio chunks from TTS WS, put into queue."""
        try:
            async for message in self._ws:
                data = json.loads(message)
                if data.get("isFinal"):
                    break
                audio = data.get("audio")
                if audio:
                    self._chunks_received += 1
                    await self._audio_queue.put(audio)
        except websockets.exceptions.ConnectionClosed:
            log.debug("tts.ws_closed_by_server", audio_chunks=self._chunks_received)
        except Exception:
            log.exception("tts.receive_error", audio_chunks=self._chunks_received)
        finally:
            # Sentinel: no more audio
            await self._audio_queue.put(None)

    async def audio_chunks(self) -> AsyncGenerator[str, None]:
        """Yield base64-encoded audio chunks as they become available."""
        while True:
            chunk = await self._audio_queue.get()
            if chunk is None:
                break
            yield chunk
