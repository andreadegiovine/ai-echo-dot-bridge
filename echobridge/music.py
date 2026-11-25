"""Musica su un Echo (modalità ESPHome), gestita dal bridge.

hassmic suona un URL alla volta: se dici "Alexa" mentre suona, la musica si interrompe e non riprende, e un annuncio
che arriva mentre suona viene scartato. Il bridge quindi:
  - converte la sorgente in MP3 con ffmpeg (YouTube dà audio in Opus/AAC, che l'Echo non legge) e la serve all'Echo
    sulla rete locale (il firewall dell'Echo non lo lascia uscire su internet);
  - la mette in pausa quando parli, quando c'è un annuncio o quando l'agente la avvia mentre sta ancora rispondendo;
  - la riprende dal punto in cui era, a risposta finita;
  - non la riprende se hai detto "Alexa, stop", o se il tool l'ha fermata o messa in pausa.
"""
import asyncio
import logging
import time

import aioesphomeapi as api

from . import audio

log = logging.getLogger("echo-bridge")
IDLE_STATES = {api.MediaPlayerState.IDLE, api.MediaPlayerState.OFF, api.MediaPlayerState.NONE}


class Music:
    def __init__(self, echo):
        self.echo = echo
        self.session = None          # dict: url, headers, title, live, position
        self.stream = None
        self.playing = False         # l'Echo la sta suonando (secondo il bridge)
        self.interrupted = False     # in pausa per voce o annuncio: riprende da sola
        self.started_at = 0.0
        self.media_idle = asyncio.Event()
        self.media_idle.set()
        self._resume_task = None
        self._idle_task = None

    # ------------------------------------------------------------ stato

    def status(self):
        if not self.session:
            return {"state": "idle", "backend": "url"}
        s = self.session
        return {"state": "playing" if self.playing else ("interrupted" if self.interrupted else "paused"),
                "title": s["title"], "url": s["url"], "position": round(self._position()), "live": s["live"], "backend": "url"}

    def _position(self):
        s = self.session
        return s["position"] + (time.monotonic() - self.started_at if self.playing else 0)

    # ------------------------------------------------------------ comandi (tool, API)

    def play(self, url, title="", headers=None, seek=0.0, live=False):
        self._halt(stop_echo=self.playing)
        self.session = {"url": url, "title": title or url, "headers": headers or {}, "live": live, "position": float(seek)}
        if self.echo.busy():
            self.interrupted = True              # l'agente sta ancora rispondendo: parte a risposta finita
            log.info("%s: musica \"%s\" in coda, parte dopo la risposta", self.echo.name, self.session["title"])
            self._schedule_resume()
        else:
            self.interrupted = False
            self._start()
        return self.status()

    def pause(self):
        if not self.session:
            return self.status()
        self._halt(stop_echo=self.playing)
        self.interrupted = False
        log.info("%s: musica in pausa a %d s", self.echo.name, self.session["position"])
        return self.status()

    def resume(self):
        if self.session and not self.playing:
            self.interrupted = False
            if self.echo.busy():
                self.interrupted = True
                self._schedule_resume()
            else:
                self._start()
        return self.status()

    def stop(self):
        self._halt(stop_echo=self.playing)
        if self.session:
            log.info("%s: musica fermata", self.echo.name)
        self.session, self.interrupted = None, False
        return self.status()

    # ------------------------------------------------------------ interno

    def _start(self):
        s = self.session
        self.stream = audio.ProcStream(s["url"], s["headers"], s["position"], s["live"])
        self.echo.cli.media_player_command(self.echo.media_player().key, media_url=self.echo.url(self.stream.path),
                                           announcement=False)
        self.started_at, self.playing = time.monotonic(), True
        self.media_idle.clear()
        log.info("%s: musica \"%s\"%s", self.echo.name, s["title"],
                 f" da {s['position']:.0f} s" if s["position"] > 1 and not s["live"] else "")

    def _halt(self, stop_echo=False):
        if self.playing:
            self.session["position"] = self._position()
            self.playing = False
        if self.stream:
            self.stream.close()
            self.stream = None
        if self._resume_task:
            self._resume_task.cancel()
            self._resume_task = None
        if stop_echo and self.echo.cli:
            self.echo.cli.media_player_command(self.echo.media_player().key, command=api.MediaPlayerCommand.STOP)

    def _schedule_resume(self):
        if self._resume_task:
            self._resume_task.cancel()
        self._resume_task = asyncio.get_running_loop().create_task(self._resume_when_free())

    async def _resume_when_free(self):
        """A risposta (o annuncio) finita: l'Echo è libero e silenzioso da un attimo, nessuna nuova richiesta."""
        try:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if not self.echo.busy() and self.media_idle.is_set():
                    await asyncio.sleep(1.5)             # una domanda di seguito parte subito dopo la risposta
                    if not self.echo.busy() and self.media_idle.is_set():
                        break
                await asyncio.sleep(0.2)
            self._resume_task = None
            if self.session and self.interrupted and not self.playing:
                self.interrupted = False
                self._start()
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------ eventi dall'Echo e dal bridge

    def on_run_start(self):
        if self.playing:                         # l'Echo ha già tagliato la musica per ascoltarti
            self._halt()
            self.interrupted = True
            log.info("%s: musica interrotta per una richiesta, riprende a %d s", self.echo.name, self.session["position"])
        if self._resume_task:
            self._resume_task.cancel()
            self._resume_task = None

    def on_run_end(self, aborted):
        if not (self.session and self.interrupted):
            return
        if aborted:                              # "Alexa, stop": la musica resta in pausa
            self.interrupted = False
            log.info("%s: \"stop\": musica in pausa a %d s", self.echo.name, self.session["position"])
            return
        self._schedule_resume()

    async def before_announce(self):
        if not self.playing:
            return
        self._halt(stop_echo=True)
        self.interrupted = True
        try:
            await asyncio.wait_for(self.media_idle.wait(), 3)
        except asyncio.TimeoutError:
            pass

    def after_announce(self):
        if self.session and self.interrupted:
            self._schedule_resume()

    def on_media_state(self, state):
        if state in IDLE_STATES:
            self.media_idle.set()
            if self.playing and not (self._idle_task and not self._idle_task.done()):
                self._idle_task = asyncio.get_running_loop().create_task(self._stopped_by_echo())
        else:
            self.media_idle.clear()

    async def _stopped_by_echo(self):
        """L'Echo ha smesso di suonare senza che il bridge lo chiedesse: fine del brano, "stop", o una richiesta."""
        await asyncio.sleep(1.5)
        if not self.playing or not self.media_idle.is_set():
            return                               # nel frattempo è partita una richiesta (on_run_start) o altro
        stream = self.stream
        self._halt()
        title = self.session["title"]
        if stream and stream.finished:
            log.info("%s: brano finito: %s", self.echo.name, title)
            self.session = None
            self._event("finished", title)
        elif stream and stream.error:
            log.error("%s: musica non riproducibile: %s", self.echo.name, stream.error)
            self.session = None
            self._event("error", title, stream.error)
        else:
            log.info("%s: musica fermata sull'Echo, in pausa a %d s", self.echo.name, self.session["position"])
            self._event("paused", title)

    def _event(self, what, title, error=""):
        """Al webhook (EVENT_WEBHOOK): così il tool musica sa quando far partire il brano successivo."""
        payload = {"echo": self.echo.name, "type": "music", "event": what, "title": title}
        if error:
            payload["error"] = error
        self.echo.hub.webhook(payload)
