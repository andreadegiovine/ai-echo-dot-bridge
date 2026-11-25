"""Audio servito via HTTP all'Echo (modalità ESPHome): risposte e annunci come WAV in streaming.

L'Echo scarica la risposta da un URL (come fa con Home Assistant) e la suona mentre arriva. Il WAV parte appena c'è
la prima frase; la lunghezza nell'intestazione è "sconosciuta" (hassmic legge fino alla fine della connessione)."""
import asyncio
import struct
import time
import uuid

from . import speech

_streams: dict[str, "AudioStream"] = {}
MAX_AGE_S = 600


def wav_header(rate, width=2, channels=1):
    size = 0xFFFFFFFF - 36
    return (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE" +
            b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * channels * width, channels * width, width * 8) +
            b"data" + struct.pack("<I", size))


class AudioStream:
    """Un flusso audio: chi produce chiama start()/chunk()/finish(), il server HTTP lo legge (anche più volte)."""

    def __init__(self, gain=1.0):
        self.id = uuid.uuid4().hex
        self.gain = gain
        self.fmt = None
        self.parts: list[bytes] = []
        self.done = False
        self.changed = asyncio.Event()
        self.created = time.monotonic()
        self.first_audio = asyncio.Event()
        _streams[self.id] = self
        _gc()

    @property
    def path(self):
        return f"/audio/{self.id}.wav"

    @property
    def started(self):
        return self.fmt is not None

    def _notify(self):
        self.changed.set()
        self.changed = asyncio.Event()

    def start(self, rate, width=2, channels=1):
        if self.fmt is None:
            self.fmt = (rate, width, channels)
            self.parts.append(wav_header(rate, width, channels))
            self._notify()

    def chunk(self, pcm: bytes, raw=False):
        if not pcm or self.done:
            return
        self.parts.append(pcm if raw else speech.attenuate(pcm, self.gain))
        self.first_audio.set()
        self._notify()

    def silence(self, seconds):
        rate, width, ch = self.fmt
        self.chunk(b"\0" * int(rate * seconds) * width * ch, raw=True)

    def finish(self):
        if not self.done:
            self.done = True
            self._notify()

    async def read(self):
        i = 0
        while True:
            while i < len(self.parts):
                yield self.parts[i]
                i += 1
            if self.done:
                return
            ev = self.changed
            await ev.wait()

    async def feed_sentence(self, text):
        """Sintetizza una frase con Piper dentro questo flusso."""
        async for kind, value in speech.synthesize(text):
            if kind == "start":
                self.start(*value)
            else:
                self.chunk(value)


class ProcStream:
    """Musica: ffmpeg legge la sorgente (es. l'URL audio di YouTube) e la converte in MP3, che l'Echo sa suonare.
    ffmpeg parte quando l'Echo apre l'URL e si ferma quando l'Echo chiude o con close()."""
    media_type = "audio/mpeg"

    def __init__(self, url, headers=None, seek=0.0, live=False):
        self.id = uuid.uuid4().hex
        self.url, self.headers, self.seek, self.live = url, headers or {}, seek, live
        self.created = time.monotonic()
        self.proc = None
        self.finished = False                     # la sorgente è finita (fine del brano), non interrotta
        self.error = ""
        _streams[self.id] = self
        _gc()

    @property
    def path(self):
        return f"/audio/{self.id}.mp3"

    def command(self):
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
        if self.headers:
            cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in self.headers.items())]
        if self.url.startswith(("http://", "https://")):
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
        if self.seek > 0 and not self.live:
            cmd += ["-ss", f"{self.seek:.1f}"]
        return cmd + ["-i", self.url, "-vn", "-ac", "2", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "192k",
                      "-f", "mp3", "pipe:1"]

    async def read(self):
        self.close()
        self.proc = proc = await asyncio.create_subprocess_exec(
            *self.command(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            while data := await proc.stdout.read(16384):
                yield data
            await proc.wait()
            if proc.returncode == 0:
                self.finished = True
            else:
                self.error = (await proc.stderr.read()).decode(errors="replace").strip()[-300:]
        finally:
            if proc.returncode is None:
                proc.kill()

    def close(self):
        if self.proc and self.proc.returncode is None:
            self.proc.kill()


def get(stream_id) -> "AudioStream | ProcStream | None":
    return _streams.get(stream_id)


def _gc():
    now = time.monotonic()
    for k in [k for k, s in _streams.items() if now - s.created > MAX_AGE_S]:
        _streams.pop(k, None)
