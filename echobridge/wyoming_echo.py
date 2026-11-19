"""Un Echo con hassmic in modalità Wyoming (PROTO=wyoming in hassmic.conf): voce, tasti e LED, niente altro.
Resta per chi la preferisce; la modalità ESPHome fa tutto questo e di più."""
import asyncio
import logging
import time

from wyoming.asr import Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.error import Error
from wyoming.event import Event
from wyoming.pipeline import RunPipeline
from wyoming.satellite import RunSatellite
from wyoming.vad import VoiceStopped

from . import config as c, speech

log = logging.getLogger("echo-bridge")


class WyomingEcho:
    proto = "wyoming"

    def __init__(self, target, hub):
        self.target, self.hub = target, hub
        self.label = self.name = target.label
        self.client = None
        self.lock = asyncio.Lock()
        self.listener = None
        self.run_task = None
        self.last_active = 0.0

    @property
    def connected(self):
        return self.client is not None

    async def send(self, event: Event):
        async with self.lock:
            await self.client.write_event(event)

    async def serve_forever(self):
        while True:
            try:
                async with AsyncTcpClient(self.target.host, self.target.port) as client:
                    self.client = client
                    await client.write_event(RunSatellite().event())
                    log.info("%s: collegato (Wyoming)", self.name)
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
        self.listener = None

    def on_event(self, ev: Event):
        if AudioChunk.is_type(ev.type):
            if self.listener is not None:
                self.listener.feed(ev.payload or b"")
        elif RunPipeline.is_type(ev.type):
            rp = RunPipeline.from_event(ev)
            if rp.start_stage.value == "wake":
                log.error("%s: l'Echo chiede la wake word al server (-w remote): non supportato, "
                          "togli -w remote da ARGS in hassmic.conf", self.name)
                return
            log.info("%s: richiesta (wake word: %s)", self.name, rp.wake_word_name or "tasto")
            self.cancel_run()                                     # tasto azione durante una risposta: si riparte
            self.listener = speech.Listener()
            self.last_active = time.monotonic()
            self.hub.last_echo = self
            self.run_task = asyncio.get_running_loop().create_task(self.run(self.listener))

    async def run(self, listener):
        try:
            pcm, tonal = await listener.result()
            self.listener = None
            whispered = c.WHISPER_DETECT and pcm is not None and tonal < c.WHISPER_RATIO
            if pcm is None:
                log.info("%s: non hai detto niente", self.name)
                await self.send(Error(text="no speech", code="stt-no-text-recognized").event())
                return
            await self.send(VoiceStopped().event())               # anello: da "ascolto" a "penso"
            text = speech.strip_wake_word(await speech.transcribe(pcm))
            log.info("%s: hai detto: %r%s", self.name, text,
                     f" (sussurrato, parlato con tono {tonal:.0%}: risposta a {c.WHISPER_GAIN_DB:+.0f} dB)" if whispered else "")
            if not text:
                await self.send(Error(text="no text", code="stt-no-text-recognized").event())
                return
            await self.send(Transcript(text=text).event())
            await self.reply(text, whispered)
        except asyncio.CancelledError:
            raise
        except Exception as e:                                    # noqa: BLE001
            log.exception("%s: errore: %s", self.name, e)
            try:
                await self.send(Error(text=str(e)[:200], code="bridge-error").event())
            except Exception:                                     # noqa: BLE001
                pass

    async def reply(self, text, whispered=False):
        gain = speech.whisper_gain() if whispered else 1.0
        messages = [{"role": "user", "content": text}]
        said, started, fmt = [], False, None

        async def begin(f):
            nonlocal started, fmt
            if not started:
                fmt, started = f, True
                await self.send(AudioStart(rate=f[0], width=f[1], channels=f[2]).event())

        async def out(pcm):
            await self.send(AudioChunk(rate=fmt[0], width=fmt[1], channels=fmt[2], audio=speech.attenuate(pcm, gain)).event())

        async def keepalive():                                    # hassmic abbandona dopo 30 s in "penso"
            await asyncio.sleep(c.KEEPALIVE_S)
            if not started:
                log.info("%s: l'agente ci mette molto, tengo viva la richiesta", self.name)
                await begin((22050, 2, 1))
                while True:
                    await self.send(AudioChunk(rate=22050, width=2, channels=1, audio=b"\0\0" * 11025).event())
                    await asyncio.sleep(0.5)

        async def speak(sentence):
            async for kind, value in speech.synthesize(sentence):
                if kind == "start":
                    if not ka.done():
                        ka.cancel()
                    await begin(value)
                else:
                    await out(value)

        ka = asyncio.get_running_loop().create_task(keepalive())
        try:
            try:
                async for sentence in speech.sentences(speech.ask_agent(messages, user=self.name)):
                    said.append(sentence)
                    log.info("%s: risposta: %s", self.name, sentence)
                    await speak(sentence)
            except (speech.httpx.HTTPError, OSError) as e:
                log.error("%s: agente non raggiungibile: %s", self.name, e)
                ka.cancel()
                said = []
                await speak(c.ERROR_PHRASE)
        finally:
            ka.cancel()
        if started:
            await self.send(AudioStop().event())
        else:
            await self.send(Error(text="empty reply", code="empty-reply").event())
        log.info("%s: fine risposta (%d frasi)", self.name, len(said))

    def describe(self, entities=False):
        return {"name": self.name, "protocol": self.proto, "address": self.label, "connected": self.connected,
                "note": "modalità Wyoming: timer, annunci ed entità solo in modalità ESPHome"}
