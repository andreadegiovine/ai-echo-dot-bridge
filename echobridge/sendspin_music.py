"""Musica via Sendspin: il bridge fa da server di musica per l'Echo, come Music Assistant (aiosendspin, la libreria di
riferimento che usa Music Assistant).

Così la musica non passa dal media player ma dal lettore Sendspin di hassmic, e l'Echo si comporta come Alexa:
  - quando dici "Alexa" o arriva un annuncio abbassa la musica (Music ducking, 16 dB di default) e poi la rialza,
    senza interromperla; le risposte e gli annunci si sentono sopra la musica
  - "Alexa, stop" la mette in pausa, il tasto azione la mette in pausa e la riprende
  - con più Echo che hanno sentito "Alexa", la abbassano tutti
L'abbinamento usa il token che l'Echo dà al bridge sul collegamento ESPHome cifrato, come fa Home Assistant con Music
Assistant: nessun passaggio a mano.
"""
import asyncio
import base64
import logging
import os
import time

from aiosendspin.models.types import MediaCommand, PairMethod
from aiosendspin.noise import FileServerPairingStore, Identity, decode_psk_token
from aiosendspin.noise.pairing import PairingAttempt
from aiosendspin.server import AudioFormat, SendspinServer
from aiosendspin.server.roles.controller.events import ControllerPauseEvent, ControllerPlayEvent, ControllerStopEvent

from . import config as c

log = logging.getLogger("echo-bridge")
RATE, CH = 48000, 2
CHUNK = RATE // 10 * CH * 2                    # 100 ms di PCM s16le stereo
FMT = AudioFormat(sample_rate=RATE, bit_depth=16, channels=CH)
BUFFER_US = 1_000_000                          # quanto audio tenere in viaggio verso l'Echo


class SendspinHub:
    """Un solo server Sendspin per tutto il bridge, con identità e abbinamenti salvati in STATE_DIR."""

    def __init__(self):
        self.server = None
        self._lock = asyncio.Lock()

    async def get(self):
        async with self._lock:
            if self.server is None:
                os.makedirs(c.STATE_DIR, mode=0o700, exist_ok=True)
                idp = os.path.join(c.STATE_DIR, "sendspin_identity")
                try:
                    ident = Identity.from_private_bytes(base64.b64decode(open(idp).read().strip()))
                except (OSError, ValueError):
                    ident = Identity.generate()
                    with open(os.open(idp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
                        f.write(base64.b64encode(ident.private_bytes).decode() + "\n")
                store = await FileServerPairingStore.open(os.path.join(c.STATE_DIR, "sendspin_pairings.json"))
                self.server = SendspinServer(asyncio.get_running_loop(), ident, "ai-echo-dot-bridge", pairing_store=store)
                await self.server.start_server(port=c.SENDSPIN_SERVER_PORT, host=c.HTTP_HOST, discover_clients=False)
                log.info("server di musica Sendspin pronto (porta %d)", c.SENDSPIN_SERVER_PORT)
            return self.server


class SendspinMusic:
    def __init__(self, echo, hub: SendspinHub):
        self.echo, self.hub = echo, hub
        self.server = None
        self.client_id = None
        self.ready = False
        self.session = None             # dict: url, headers, title, live, position
        self.state = "idle"             # idle | playing | paused
        self._feeder = None
        self._started_at = 0.0
        self._sent_s = 0.0
        self._group = None

    # ------------------------------------------------------------ abbinamento

    async def setup(self):
        """Dopo il collegamento ESPHome: token dall'Echo, abbinamento (la prima volta), lettore pronto."""
        try:
            tok_resp = await asyncio.wait_for(self.echo.cli.sendspin_pairing_token(), 5)
        except Exception as e:                                    # noqa: BLE001 - Echo senza Sendspin (-z 0) o vecchio
            log.info("%s: Sendspin non disponibile (%s): musica dal media player", self.echo.name, e or type(e).__name__)
            return
        if not tok_resp.token:
            log.info("%s: Sendspin non disponibile (%s): musica dal media player", self.echo.name, tok_resp.status)
            return
        tok = decode_psk_token(tok_resp.token)
        self.client_id = tok.client_id
        self.server = await self.hub.get()
        paired = any(r.client_id == tok.client_id for r in await self.server.pairing_store.list_records())
        attempt = None if paired else PairingAttempt(method=PairMethod.PAIRING_PSK, pairing_psk=tok.pairing_psk,
                                                     client_id=tok.client_id)
        url = f"ws://{self.echo.target.host}:{c.SENDSPIN_ECHO_PORT}/sendspin"
        self.server.connect_to_client(url, retry_initial_connection=True, retry_indefinitely=True, pairing_attempt=attempt)
        for _ in range(200):
            cl = self.server.get_client(self.client_id)
            if cl is not None and cl.role("player@v1") is not None:
                break
            await asyncio.sleep(0.1)
        else:
            log.warning("%s: il lettore Sendspin non risponde: musica dal media player", self.echo.name)
            return
        self.ready = True
        log.info("%s: lettore Sendspin %s: la musica si abbassa da sola per voce e annunci",
                 self.echo.name, "abbinato" if not paired else "collegato")

    def _client(self):
        cl = self.server.get_client(self.client_id) if self.server else None
        if cl is None or cl.role("player@v1") is None:
            raise RuntimeError(f"il lettore Sendspin di {self.echo.name} non è collegato")
        return cl

    def _watch(self, group):
        if group is self._group:
            return
        self._group = group
        ctl = group.group_role("controller")
        if ctl is not None:
            ctl.set_supported_commands([MediaCommand.PLAY, MediaCommand.PAUSE, MediaCommand.STOP])
        group.add_event_listener(self._on_group_event)

    def _on_group_event(self, group, ev):
        if group is not self._group:
            return
        if isinstance(ev, ControllerPauseEvent) and self.state == "playing":
            log.info("%s: pausa dall'Echo (\"stop\" o tasto)", self.echo.name)
            self.pause()
        elif isinstance(ev, ControllerPlayEvent) and self.state == "paused":
            log.info("%s: ripresa dall'Echo (tasto)", self.echo.name)
            self.resume()
        elif isinstance(ev, ControllerStopEvent):
            self.stop()

    # ------------------------------------------------------------ comandi (stessa interfaccia di music.Music)

    def status(self):
        if not self.session:
            return {"state": "idle", "backend": "sendspin"}
        s = self.session
        return {"state": self.state, "title": s["title"], "url": s["url"], "position": round(self._position()),
                "live": s["live"], "backend": "sendspin"}

    def _position(self):
        if self.state != "playing":
            return self.session["position"]
        played = min(self._sent_s, time.monotonic() - self._started_at)
        return self.session["position"] + max(0.0, played)

    def play(self, url, title="", headers=None, seek=0.0, live=False):
        self.session = {"url": url, "title": title or url, "headers": headers or {}, "live": live, "position": float(seek)}
        self._start()
        return self.status()

    def pause(self):
        if self.session and self.state == "playing":
            self.session["position"] = self._position()
            self._cancel()
            self.state = "paused"
            log.info("%s: musica in pausa a %d s", self.echo.name, self.session["position"])
            self._event("paused")
        return self.status()

    def resume(self):
        if self.session and self.state != "playing":
            self._start()
        return self.status()

    def stop(self):
        if self.session:
            log.info("%s: musica fermata", self.echo.name)
        self._cancel()
        self.session, self.state = None, "idle"
        return self.status()

    # ------------------------------------------------------------ flusso

    def _start(self):
        cl = self._client()
        self._watch(cl.group)
        prev = self._cancel()
        self.state = "playing"
        self._feeder = asyncio.get_running_loop().create_task(self._feed(cl, dict(self.session), prev))
        s = self.session
        log.info("%s: musica \"%s\" (Sendspin)%s", self.echo.name, s["title"],
                 f" da {s['position']:.0f} s" if s["position"] > 1 and not s["live"] else "")

    def _cancel(self):
        """Ferma il flusso in corso; restituisce il suo task, che il successivo aspetta (chiude il suo stream)."""
        prev, self._feeder = self._feeder, None
        if prev and not prev.done():
            prev.cancel()
        return prev

    def _ffmpeg(self, s):
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
        if s["headers"]:
            cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in s["headers"].items())]
        if s["url"].startswith(("http://", "https://")):
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
        if s["position"] > 0 and not s["live"]:
            cmd += ["-ss", f"{s['position']:.1f}"]
        return cmd + ["-i", s["url"], "-vn", "-ac", str(CH), "-ar", str(RATE), "-f", "s16le", "pipe:1"]

    async def _feed(self, client, s, prev=None):
        if prev is not None:
            await asyncio.gather(prev, return_exceptions=True)
        proc = await asyncio.create_subprocess_exec(*self._ffmpeg(s), stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        group = client.group
        finished = error = None
        try:
            stream = group.start_stream()
            self._started_at, self._sent_s = time.monotonic(), 0.0
            pending = b""
            while True:
                data = await proc.stdout.read(CHUNK)
                if not data:
                    break
                pending += data
                if len(pending) < CHUNK:
                    continue
                n = len(pending) // 4 * 4
                stream.prepare_audio(pending[:n], FMT)
                pending = pending[n:]
                await stream.commit_audio()
                self._sent_s += n / (RATE * CH * 2)
                await stream.sleep_to_limit_buffer(BUFFER_US)
            await proc.wait()
            if proc.returncode == 0 and self._sent_s > 0:
                await asyncio.sleep(BUFFER_US / 1e6 + 0.3)        # quello che è ancora in viaggio
                finished = True
            else:
                error = (await proc.stderr.read()).decode(errors="replace").strip()[-300:] or "nessun audio"
        except asyncio.CancelledError:
            raise
        except Exception as e:                                    # noqa: BLE001
            error = str(e)
        finally:
            if proc.returncode is None:
                proc.kill()
            try:
                await group.stop()
            except Exception:                                     # noqa: BLE001
                pass
        if asyncio.current_task() is not self._feeder:
            return
        title = s["title"]
        self._feeder, self.session, self.state = None, None, "idle"
        if finished:
            log.info("%s: brano finito: %s", self.echo.name, title)
            self._event("finished", title)
        else:
            log.error("%s: musica non riproducibile: %s", self.echo.name, error)
            self._event("error", title, error)

    def _event(self, what, title=None, error=""):
        payload = {"echo": self.echo.name, "type": "music", "event": what,
                   "title": title or (self.session or {}).get("title", "")}
        if error:
            payload["error"] = error
        self.echo.hub.webhook(payload)
