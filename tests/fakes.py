#!/usr/bin/env python3
"""Server finti per il test: STT (Wyoming), TTS (Wyoming), agente (OpenAI SSE). Registra cosa riceve in fakes.log."""
import asyncio, json, sys
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.server import AsyncEventHandler, AsyncServer
from wyoming.tts import Synthesize

LOG = open(sys.argv[1], "a", buffering=1)
SLOW = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0      # secondi prima della prima parola dell'agente


class Stt(AsyncEventHandler):
    n = 0
    async def handle_event(self, ev):
        if AudioChunk.is_type(ev.type):
            self.n += len(ev.payload)
        elif Transcribe.is_type(ev.type):
            LOG.write(f"stt: transcribe lang={Transcribe.from_event(ev).language}\n")
        elif AudioStop.is_type(ev.type):
            LOG.write(f"stt: {self.n} bytes ({self.n / 32000:.2f} s)\n")
            await self.write_event(Transcript(text="Alexa, che tempo fa domani?").event())
            return False
        return True


class Tts(AsyncEventHandler):
    async def handle_event(self, ev):
        if Synthesize.is_type(ev.type):
            s = Synthesize.from_event(ev)
            LOG.write(f"tts: {s.text!r} voice={s.voice.name if s.voice else None}\n")
            await self.write_event(AudioStart(rate=22050, width=2, channels=1).event())
            for _ in range(max(1, len(s.text) // 10)):                     # ~0.1 s di audio ogni 10 caratteri
                await self.write_event(AudioChunk(rate=22050, width=2, channels=1, audio=b"\x10\x00" * 2205).event())
            await self.write_event(AudioStop().event())
            return False
        return True


async def llm(reader, writer):
    head = await reader.readuntil(b"\r\n\r\n")
    length = int([l for l in head.decode().split("\r\n") if l.lower().startswith("content-length")][0].split(":")[1])
    body = json.loads(await reader.readexactly(length))
    LOG.write(f"llm: messages={json.dumps(body['messages'], ensure_ascii=False)} stream={body['stream']}\n")
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n")
    await asyncio.sleep(SLOW)
    for piece in ["<think>devo usare lo strumento meteo</think>", "Domani a Roma ", "**sole** e 22 gradi. ",
                  "Ti serve", " altro?"]:
        writer.write(f"data: {json.dumps({'choices': [{'delta': {'content': piece}}]})}\n\n".encode())
        await writer.drain(); await asyncio.sleep(0.05)
    writer.write(b"data: [DONE]\n\n"); await writer.drain(); writer.close()


async def main():
    stt = AsyncServer.from_uri("tcp://127.0.0.1:10300")
    tts = AsyncServer.from_uri("tcp://127.0.0.1:10200")
    http = await asyncio.start_server(llm, "127.0.0.1", 18080)
    await asyncio.gather(stt.run(lambda r, w: Stt(r, w)), tts.run(lambda r, w: Tts(r, w)), http.serve_forever())

asyncio.run(main())
