#!/usr/bin/env python3
"""echo-bridge (https://github.com/andreadegiovine/ai-echo-dot-bridge): collega uno o più Echo sbloccati (hassmic, PROTO=wyoming) a un agente LLM locale, senza Home Assistant.

Flusso per ogni richiesta:
  Echo ("Alexa" o tasto azione) --run-pipeline--> bridge
  Echo --audio-chunk 16 kHz--> bridge (VAD: capisce quando hai finito di parlare)
  bridge --audio--> faster-whisper (Wyoming)  --> testo
  bridge --testo--> agente (API OpenAI-compatibile, streaming) --> frasi
  bridge --frasi--> Piper (Wyoming) --> audio --> Echo (riproduzione)

Configurazione: variabili d'ambiente (vedi echo-bridge.env).
"""
import asyncio
import json
import logging
import os
import re
import time

import httpx
import numpy as np
import webrtcvad
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncClient, AsyncTcpClient
from wyoming.error import Error
from wyoming.event import Event
from wyoming.pipeline import RunPipeline
from wyoming.satellite import RunSatellite
from wyoming.snd import Played
from wyoming.tts import Synthesize, SynthesizeVoice
from wyoming.vad import VoiceStopped


def env(name, default=None, cast=str):
    v = os.environ.get(name)
    return default if v in (None, "") else cast(v)


ECHO_HOSTS = [h.strip() for h in env("ECHO_HOSTS", "").split(",") if h.strip()]   # "192.168.1.50" o "ip:porta"
STT_URI = env("STT_URI", "tcp://127.0.0.1:10300")
STT_LANG = env("STT_LANG", "it")
TTS_URI = env("TTS_URI", "tcp://127.0.0.1:10200")
TTS_VOICE = env("TTS_VOICE", "it_IT-paola-medium")
LLM_URL = env("LLM_URL", "http://127.0.0.1:8080/v1/chat/completions")
LLM_MODEL = env("LLM_MODEL", "local")
LLM_API_KEY = env("LLM_API_KEY", "")
LLM_STREAM = env("LLM_STREAM", "1") == "1"
LLM_TIMEOUT = env("LLM_TIMEOUT", 120.0, float)
LLM_EXTRA = json.loads(env("LLM_EXTRA_JSON", "{}"))             # campi extra nel body, es. {"temperature":0.6}
VAD_MODE = env("VAD_MODE", 2, int)                               # 0..3, più alto = più severo nel riconoscere voce
SILENCE_MS = env("SILENCE_MS", 800, int)                         # pausa che chiude la richiesta
NO_SPEECH_S = env("NO_SPEECH_S", 6.0, float)                     # nessuna voce entro N s -> annulla in silenzio
MAX_REQUEST_S = env("MAX_REQUEST_S", 15.0, float)
KEEPALIVE_S = env("KEEPALIVE_S", 20.0, float)                    # hassmic abbandona dopo 30 s in "pensando"
ERROR_PHRASE = env("ERROR_PHRASE", "Scusa, non riesco a raggiungere l'agente.")
WHISPER_DETECT = env("WHISPER_DETECT", "1") == "1"                # se sussurri, risponde a volume basso
WHISPER_RATIO = env("WHISPER_RATIO", 0.2, float)                 # sotto questa quota di parlato "con tono" = sussurro
WHISPER_GAIN_DB = env("WHISPER_GAIN_DB", -12.0, float)           # attenuazione della risposta sussurrata

RATE, WIDTH, CH = 16000, 2, 1
FRAME = 480 * WIDTH                                              # 30 ms a 16 kHz per webrtcvad
SENTENCE_END = re.compile(r"(.+?[.!?;:…])(\s+|$)|(.+?)\n+", re.S)

log = logging.getLogger("echo-bridge")


def is_voiced(frame: bytes) -> bool:
    """True se il frame ha un tono (corde vocali che vibrano, 70-400 Hz). Il sussurro non ne ha: è solo fruscio.
    Si misura il tono, non il volume, perché l'Echo alza da solo il guadagno di chi parla piano."""
    x = np.frombuffer(frame, dtype="<i2").astype(np.float32)
    x -= x.mean()
    c = np.correlate(x, x, "full")[len(x) - 1:]                 # autocorrelazione, ritardi 0..n-1
    e = np.cumsum(x * x)
    lags = np.arange(RATE // 400, RATE // 70 + 1)               # 40..228 campioni
    head = e[len(x) - 1 - lags]                                 # energia di x[:-lag]
    tail = e[-1] - e[lags - 1]                                  # energia di x[lag:]
    norm = np.sqrt(np.maximum(head * tail, 1e-6))
    return float(np.max(c[lags] / norm)) > 0.5


def attenuate(pcm: bytes, gain: float) -> bytes:
    return (np.frombuffer(pcm, dtype="<i2").astype(np.float32) * gain).astype("<i2").tobytes()


def clean_for_speech(text):
    text = re.sub(r"<think>.*?</think>", " ", text, flags=re.S)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[*_`#>|~]+", " ", text)
    text = re.sub(r"[\U0001F000-\U0001FAFF☀-➿]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------- servizi esterni

async def transcribe(pcm: bytes) -> str:
    async with AsyncClient.from_uri(STT_URI) as stt:
        await stt.write_event(Transcribe(language=STT_LANG).event())
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
    """Restituisce (rate, width, channels) e poi i blocchi audio di Piper per una frase."""
    async with AsyncClient.from_uri(TTS_URI) as tts:
        await tts.write_event(Synthesize(text=text, voice=SynthesizeVoice(name=TTS_VOICE)).event())
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


async def ask_agent(messages):
    """Testo dell'agente, a pezzi man mano che arriva (streaming SSE) o tutto insieme."""
    headers = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"
    body = {"model": LLM_MODEL, "messages": messages, "stream": LLM_STREAM, **LLM_EXTRA}
    async with httpx.AsyncClient(timeout=httpx.Timeout(LLM_TIMEOUT, connect=10)) as http:
        if not LLM_STREAM:
            r = await http.post(LLM_URL, headers=headers, json=body)
            r.raise_for_status()
            yield r.json()["choices"][0]["message"].get("content") or ""
            return
        async with http.stream("POST", LLM_URL, headers=headers, json=body) as r:
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
                if delta.get("content"):                    # reasoning_content (modelli "thinking") viene ignorato
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


# ---------------------------------------------------------------- un Echo

class EchoLink:
    def __init__(self, host: str):
        self.host, _, port = host.partition(":")
        self.port = int(port or 16700)
        self.name = host
        self.client = None
        self.lock = asyncio.Lock()
        self.mic = None                     # asyncio.Queue mentre ascolta
        self.run_task = None

    async def send(self, event: Event):
        async with self.lock:
            await self.client.write_event(event)

    async def serve_forever(self):
        while True:
            try:
                async with AsyncTcpClient(self.host, self.port) as client:
                    self.client = client
                    await client.write_event(RunSatellite().event())
                    log.info("%s: collegato", self.name)
                    while (ev := await client.read_event()) is not None:
                        self.on_event(ev)
                log.warning("%s: connessione chiusa", self.name)
            except (OSError, asyncio.IncompleteReadError) as e:
                log.warning("%s: non raggiungibile (%s)", self.name, e)
            finally:
                self.cancel_run()
                self.client = None
            await asyncio.sleep(5)

    def cancel_run(self):
        if self.run_task and not self.run_task.done():
            self.run_task.cancel()
        self.mic = None

    def on_event(self, ev: Event):
        if AudioChunk.is_type(ev.type):
            if self.mic is not None:
                self.mic.put_nowait(ev.payload or b"")
        elif RunPipeline.is_type(ev.type):
            rp = RunPipeline.from_event(ev)
            if rp.start_stage.value == "wake":
                log.error("%s: l'Echo chiede la wake word al server (-w remote): non supportato, "
                          "togli -w remote da ARGS in hassmic.conf", self.name)
                return
            log.info("%s: richiesta (wake word: %s)", self.name, rp.wake_word_name or "tasto")
            self.cancel_run()                                   # tasto azione durante una risposta: si riparte
            self.mic = asyncio.Queue()
            self.run_task = asyncio.create_task(self.run(self.mic))
        elif Played.is_type(ev.type):
            log.debug("%s: riproduzione finita", self.name)
        elif ev.type == "pong":
            pass

    # ------------------------------------------------------------ una richiesta

    async def listen(self, mic: asyncio.Queue):
        """(audio dal wake word fino a fine frase, quota di parlato con tono); audio None se non si è detto niente."""
        vad = webrtcvad.Vad(VAD_MODE)
        audio, pending = bytearray(), b""
        voiced_run, speech, silence_ms = 0, False, 0
        speech_frames = tonal_frames = 0

        def result():
            return (bytes(audio) if speech else None), (tonal_frames / speech_frames if speech_frames else 1.0)
        start = time.monotonic()
        while True:
            left = (NO_SPEECH_S if not speech else MAX_REQUEST_S) - (time.monotonic() - start)
            if left <= 0:
                return result()
            try:
                data = await asyncio.wait_for(mic.get(), timeout=left)
            except asyncio.TimeoutError:
                continue
            audio += data
            pending += data
            while len(pending) >= FRAME:
                frame, pending = pending[:FRAME], pending[FRAME:]
                if vad.is_speech(frame, RATE):
                    if WHISPER_DETECT:
                        speech_frames += 1
                        tonal_frames += is_voiced(frame)
                    voiced_run += 1
                    silence_ms = 0
                    if voiced_run >= 5:                         # 150 ms di voce: stai parlando
                        speech = True
                else:
                    voiced_run = 0
                    silence_ms += 30
                    if speech and silence_ms >= SILENCE_MS:
                        return result()

    async def run(self, mic):
        try:
            pcm, tonal = await self.listen(mic)
            self.mic = None
            whispered = WHISPER_DETECT and pcm is not None and tonal < WHISPER_RATIO
            if pcm is None:
                log.info("%s: non hai detto niente", self.name)
                await self.send(Error(text="no speech", code="stt-no-text-recognized").event())
                return
            await self.send(VoiceStopped().event())             # anello: da "ascolto" a "penso"
            text = await transcribe(pcm)
            text = re.sub(r"^\W*alexa\W*", "", text, flags=re.I).strip()
            log.info("%s: hai detto: %r%s", self.name, text,
                     f" (sussurrato, parlato con tono {tonal:.0%}: risposta a {WHISPER_GAIN_DB:+.0f} dB)" if whispered else "")
            if not text:
                await self.send(Error(text="no text", code="stt-no-text-recognized").event())
                return
            await self.send(Transcript(text=text).event())
            await self.reply(text, whispered)
        except asyncio.CancelledError:
            raise
        except Exception as e:                                  # noqa: BLE001 - qualsiasi guasto: avvisa l'Echo
            log.exception("%s: errore: %s", self.name, e)
            try:
                await self.send(Error(text=str(e)[:200], code="bridge-error").event())
            except Exception:                                   # noqa: BLE001
                pass

    async def reply(self, text, whispered=False):
        gain = 10 ** (WHISPER_GAIN_DB / 20) if whispered else 1.0
        messages = [{"role": "user", "content": text}]         # prompt di sistema, contesto e strumenti: li gestisce llama.cpp
        said, started, fmt = [], False, None

        async def begin(f):
            nonlocal started, fmt
            if not started:
                fmt, started = f, True
                await self.send(AudioStart(rate=f[0], width=f[1], channels=f[2]).event())

        async def keepalive():                                  # agente lento (strumenti): silenzio per non scadere
            await asyncio.sleep(KEEPALIVE_S)
            if not started:
                log.info("%s: l'agente ci mette molto, tengo viva la richiesta", self.name)
                await begin((22050, 2, 1))
                while True:
                    await self.send(AudioChunk(rate=22050, width=2, channels=1, audio=b"\0\0" * 11025).event())
                    await asyncio.sleep(0.5)

        ka = asyncio.create_task(keepalive())
        try:
            try:
                async for sentence in sentences(ask_agent(messages)):
                    said.append(sentence)
                    log.info("%s: risposta: %s", self.name, sentence)
                    async for kind, value in synthesize(sentence):
                        if kind == "start":
                            if not ka.done():
                                ka.cancel()
                            await begin(value)
                        elif kind == "chunk":
                            await self.send(AudioChunk(rate=fmt[0], width=fmt[1], channels=fmt[2], audio=attenuate(value, gain) if gain != 1.0 else value).event())
            except (httpx.HTTPError, OSError) as e:
                log.error("%s: agente non raggiungibile: %s", self.name, e)
                ka.cancel()
                async for kind, value in synthesize(ERROR_PHRASE):
                    if kind == "start":
                        await begin(value)
                    else:
                        await self.send(AudioChunk(rate=fmt[0], width=fmt[1], channels=fmt[2], audio=attenuate(value, gain) if gain != 1.0 else value).event())
                said = []
        finally:
            ka.cancel()
        if started:
            await self.send(AudioStop().event())
        else:
            await self.send(Error(text="empty reply", code="empty-reply").event())
        log.info("%s: fine risposta (%d frasi)", self.name, len(said))


async def main():
    logging.basicConfig(level=env("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    if not ECHO_HOSTS:
        raise SystemExit("ECHO_HOSTS non impostato (es. ECHO_HOSTS=192.168.1.50)")
    log.info("Echo: %s | STT %s | TTS %s (%s) | agente %s", ECHO_HOSTS, STT_URI, TTS_URI, TTS_VOICE, LLM_URL)
    await asyncio.gather(*(EchoLink(h).serve_forever() for h in ECHO_HOSTS))


if __name__ == "__main__":
    asyncio.run(main())
