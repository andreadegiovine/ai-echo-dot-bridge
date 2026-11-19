"""Servizi vocali e agente: STT (Wyoming), TTS (Wyoming), agente OpenAI-compatibile, fine frase, sussurro."""
import asyncio
import json
import re
import time

import httpx
import numpy as np
import webrtcvad
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncClient
from wyoming.error import Error
from wyoming.tts import Synthesize, SynthesizeVoice

from . import config as c

RATE, WIDTH, CH = 16000, 2, 1
FRAME = 480 * WIDTH                                              # 30 ms a 16 kHz per webrtcvad
SENTENCE_END = re.compile(r"(.+?[.!?;:…])(\s+|$)|(.+?)\n+", re.S)
END_OF_STREAM = object()


def clean_for_speech(text):
    text = re.sub(r"<think>.*?</think>", " ", text, flags=re.S)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[*_`#>|~]+", " ", text)
    text = re.sub("[\U0001F000-\U0001FAFF☀-➿]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def strip_wake_word(text):
    return re.sub(r"^\W*alexa\W*", "", text, flags=re.I).strip()


def is_voiced(frame: bytes) -> bool:
    """True se il frame ha un tono (corde vocali che vibrano, 70-400 Hz). Il sussurro non ne ha: è solo fruscio.
    Si misura il tono, non il volume, perché l'Echo alza da solo il guadagno di chi parla piano."""
    x = np.frombuffer(frame, dtype="<i2").astype(np.float32)
    x -= x.mean()
    cor = np.correlate(x, x, "full")[len(x) - 1:]
    e = np.cumsum(x * x)
    lags = np.arange(RATE // 400, RATE // 70 + 1)
    head = e[len(x) - 1 - lags]
    tail = e[-1] - e[lags - 1]
    norm = np.sqrt(np.maximum(head * tail, 1e-6))
    return float(np.max(cor[lags] / norm)) > 0.5


def attenuate(pcm: bytes, gain: float) -> bytes:
    if gain == 1.0:
        return pcm
    return (np.frombuffer(pcm, dtype="<i2").astype(np.float32) * gain).astype("<i2").tobytes()


def whisper_gain():
    return 10 ** (c.WHISPER_GAIN_DB / 20)


class Listener:
    """Ascolta il microfono finché hai finito di parlare. feed() con i blocchi audio, end() se l'Echo dice che hai
    finito (fine richiesta sentita sull'Echo), poi await result() -> (pcm o None, quota di parlato con tono)."""

    def __init__(self):
        self.vad = webrtcvad.Vad(c.VAD_MODE)
        self.q = asyncio.Queue()
        self.on_speech = None                                    # callback al primo parlato (STT_VAD_START)

    def feed(self, data: bytes):
        self.q.put_nowait(data)

    def end(self):
        self.q.put_nowait(END_OF_STREAM)

    async def result(self):
        audio, pending = bytearray(), b""
        voiced_run, speech, silence_ms = 0, False, 0
        speech_frames = tonal_frames = 0
        start = time.monotonic()

        def done():
            return (bytes(audio) if speech else None), (tonal_frames / speech_frames if speech_frames else 1.0)

        while True:
            left = (c.NO_SPEECH_S if not speech else c.MAX_REQUEST_S) - (time.monotonic() - start)
            if left <= 0:
                return done()
            try:
                data = await asyncio.wait_for(self.q.get(), timeout=left)
            except asyncio.TimeoutError:
                continue
            if data is END_OF_STREAM:
                return done()
            audio += data
            pending += data
            while len(pending) >= FRAME:
                frame, pending = pending[:FRAME], pending[FRAME:]
                if self.vad.is_speech(frame, RATE):
                    if c.WHISPER_DETECT:
                        speech_frames += 1
                        tonal_frames += is_voiced(frame)
                    voiced_run += 1
                    silence_ms = 0
                    if voiced_run >= 5 and not speech:              # 150 ms di voce: stai parlando
                        speech = True
                        if self.on_speech:
                            self.on_speech()
                else:
                    voiced_run = 0
                    silence_ms += 30
                    if speech and silence_ms >= c.SILENCE_MS:
                        return done()



async def transcribe(pcm: bytes) -> str:
    async with AsyncClient.from_uri(c.STT_URI) as stt:
        await stt.write_event(Transcribe(language=c.STT_LANG).event())
        await stt.write_event(AudioStart(rate=RATE, width=WIDTH, channels=CH).event())
        for i in range(0, len(pcm), 3200):
            await stt.write_event(AudioChunk(rate=RATE, width=WIDTH, channels=CH, audio=pcm[i:i + 3200]).event())
        await stt.write_event(AudioStop().event())
        while True:
            ev = await stt.read_event()
            if ev is None:
                raise ConnectionError("STT ha chiuso la connessione")
            if Transcript.is_type(ev.type):
                return Transcript.from_event(ev).text.strip()


async def synthesize(text: str):
    """("start", (rate, width, channels)) e poi ("chunk", bytes) per una frase, da Piper."""
    async with AsyncClient.from_uri(c.TTS_URI) as tts:
        await tts.write_event(Synthesize(text=text, voice=SynthesizeVoice(name=c.TTS_VOICE)).event())
        while True:
            ev = await tts.read_event()
            if ev is None or AudioStop.is_type(ev.type):
                return
            if AudioStart.is_type(ev.type):
                s = AudioStart.from_event(ev)
                yield ("start", (s.rate, s.width, s.channels))
            elif AudioChunk.is_type(ev.type):
                yield ("chunk", AudioChunk.from_event(ev).audio)
            elif Error.is_type(ev.type):
                raise RuntimeError(f"TTS: {Error.from_event(ev).text}")


async def ask_agent(messages, user=None):
    """Testo dell'agente, a pezzi man mano che arriva (streaming SSE) o tutto insieme."""
    headers = {"Content-Type": "application/json"}
    if c.LLM_API_KEY:
        headers["Authorization"] = f"Bearer {c.LLM_API_KEY}"
    body = {"model": c.LLM_MODEL, "messages": messages, "stream": c.LLM_STREAM, **c.LLM_EXTRA}
    if user:
        body["user"] = user                                      # quale Echo ha chiesto (campo standard OpenAI)
    async with httpx.AsyncClient(timeout=httpx.Timeout(c.LLM_TIMEOUT, connect=10)) as http:
        if not c.LLM_STREAM:
            r = await http.post(c.LLM_URL, headers=headers, json=body)
            r.raise_for_status()
            yield r.json()["choices"][0]["message"].get("content") or ""
            return
        async with http.stream("POST", c.LLM_URL, headers=headers, json=body) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    delta = json.loads(data)["choices"][0].get("delta") or {}
                except (ValueError, KeyError, IndexError):
                    continue
                if delta.get("content"):                         # reasoning_content (modelli "thinking") ignorato
                    yield delta["content"]


async def sentences(pieces):
    """Raggruppa lo stream dell'agente in frasi, così Piper parte prima che l'agente abbia finito."""
    buf, in_think = "", False
    async for piece in pieces:
        buf += piece
        if "<think>" in buf:
            in_think = True
        if in_think:
            if "</think>" not in buf:
                continue
            buf, in_think = buf.split("</think>", 1)[1], False
        while (m := SENTENCE_END.match(buf)) and len(m.group(0)) >= 12:
            buf = buf[m.end():]
            if s := clean_for_speech(m.group(1) or m.group(3)):
                yield s
    if not in_think and (s := clean_for_speech(buf)):
        yield s
