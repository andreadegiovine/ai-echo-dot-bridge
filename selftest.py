#!/usr/bin/env python3
"""Autotest del container, senza Echo: Piper dice una frase, faster-whisper la trascrive, l'agente risponde.
Uso:  set -a; . /etc/echo-bridge/echo-bridge.env; set +a; /opt/echo-bridge/venv/bin/python selftest.py"""
import asyncio, os, sys, time

sys.path.insert(0, os.environ.get("BRIDGE_DIR", "/opt/echo-bridge"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import echo_bridge as b

PHRASE = "Accendi la luce del soggiorno, per favore."


def to_16k(pcm: bytes, rate: int) -> bytes:
    src = memoryview(pcm).cast("h")
    n = int(len(src) * 16000 / rate)
    out = bytearray()
    for i in range(n):
        x = i * rate / 16000; j = int(x); f = x - j
        a = src[j]; c = src[j + 1] if j + 1 < len(src) else a
        out += int(a + (c - a) * f).to_bytes(2, "little", signed=True)
    return bytes(out)


async def main():
    t = time.monotonic(); audio, rate = bytearray(), 22050
    async for kind, value in b.synthesize(PHRASE):
        if kind == "start": rate = value[0]
        else: audio += value
    print(f"TTS  ok: {len(audio) / 2 / rate:.1f} s di audio a {rate} Hz in {time.monotonic() - t:.1f} s")

    t = time.monotonic()
    text = await b.transcribe(b"\0\0" * 8000 + to_16k(bytes(audio), rate) + b"\0\0" * 8000)
    print(f"STT  ok: {text!r} in {time.monotonic() - t:.1f} s")

    t = time.monotonic(); first = None; parts = []
    msgs = [{"role": "user", "content": "Rispondi solo: tutto funziona."}]
    async for s in b.sentences(b.ask_agent(msgs)):
        first = first or time.monotonic() - t; parts.append(s)
    print(f"LLM  ok: {' '.join(parts)!r} (prima frase dopo {first or 0:.1f} s, totale {time.monotonic() - t:.1f} s)")

asyncio.run(main())
