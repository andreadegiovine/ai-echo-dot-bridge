"""Un Echo con hassmic in modalità ESPHome (default): il bridge fa la parte di Home Assistant.

Cosa replica di Home Assistant, con lo stesso protocollo (aioesphomeapi, la libreria di Home Assistant):
  - chiave di cifratura: generata e impostata al primo collegamento, poi solo il bridge entra
  - ora e fuso orario, posizione di casa (fuso automatico dell'Echo)
  - pipeline vocale: microfono -> fine frase -> STT -> agente -> TTS in streaming via HTTP
  - sussurro: il sensore nativo "Last request whispered" (modello Amazon) se c'è, altrimenti la stima locale
  - domande di seguito (continue_conversation): se la risposta finisce con "?" l'Echo riascolta senza "Alexa"
  - timer (anello e suoneria dell'Echo), annunci, annunci con domanda (start_conversation), media player, volume
  - azioni che l'Echo chiede a Home Assistant: tag di arbitraggio, chiavi tra Echo, annunci Bluetooth
  - entità (interruttori, numeri, selettori, luce dell'anello, aggiornamenti) ed eventi (tasti, wake word)
"""
import asyncio
import base64
import dataclasses
import enum
import logging
import os
import socket
import time

import aioesphomeapi as api
from aioesphomeapi import VoiceAssistantEventType as Ev

from . import audio, config as c, speech
from .music import Music
from .planner import Planner

log = logging.getLogger("echo-bridge")


def _jsonable(obj):
    if dataclasses.is_dataclass(obj):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, enum.Enum):
        return obj.name
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    return obj


class KeyStore:
    """Chiavi API degli Echo (come le tiene Home Assistant): STATE_DIR/keys/<host_porta>, permessi 600."""

    def __init__(self):
        self.dir = os.path.join(c.STATE_DIR, "keys")

    def _path(self, label):
        return os.path.join(self.dir, label.replace(":", "_").replace("/", "_"))

    def get(self, label):
        try:
            with open(self._path(label)) as f:
                return f.read().strip() or None
        except OSError:
            return None

    def put(self, label, key):
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        p = self._path(label)
        with open(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            f.write(key + "\n")

    def drop(self, label):
        try:
            os.remove(self._path(label))
        except OSError:
            pass


class EsphomeEcho:
    proto = "esphome"

    def __init__(self, target, hub):
        self.target, self.hub = target, hub
        self.label = target.label
        self.name = target.label                    # sostituito dal nome dell'Echo appena collegato
        self.node = None
        self.cli: api.APIClient | None = None
        self.connected = False
        self.info = None
        self.entities: dict[int, object] = {}
        self.by_id: dict[str, object] = {}
        self.services: dict[str, api.UserService] = {}
        self.states: dict[int, object] = {}
        self.state_seq: dict[int, int] = {}
        self.state_subs: set[tuple[str, str | None]] = set()
        self.planner = Planner(self, hub.store)
        self.run_task = None
        self.listener = None
        self.followup = None                        # (domanda, risposta, quando) se l'ultima risposta era una domanda
        self.last_active = 0.0
        self.keys = KeyStore()
        self.url_music = Music(self)                  # musica dal media player (pausa e ripresa fatte dal bridge)
        self.ss_music = None                          # musica via Sendspin (l'Echo la abbassa da solo)
        if hub.sendspin is not None:
            from .sendspin_music import SendspinMusic
            self.ss_music = SendspinMusic(self, hub.sendspin)
        self._abort = False
        self._announcing = 0

    @property
    def music(self):
        return self.ss_music if self.ss_music is not None and self.ss_music.ready else self.url_music

    def busy(self):
        """Una richiesta vocale o un annuncio in corso."""
        return bool(self._announcing) or bool(self.run_task and not self.run_task.done())

    # ------------------------------------------------------------ collegamento

    def local_host(self):
        if c.PUBLIC_HOST:
            return c.PUBLIC_HOST
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((self.target.host, self.target.port))
            return s.getsockname()[0]

    def url(self, path):
        return f"http://{self.local_host()}:{c.HTTP_PORT}{path}"

    async def _provision(self):
        """Come Home Assistant: collegamento in chiaro, poi la chiave su un canale Noise con PSK a zeri."""
        probe = api.APIClient(self.target.host, self.target.port, None, client_info="ai-echo-dot-bridge")
        await probe.connect(login=True)
        info = await probe.device_info()
        await probe.disconnect()
        if not info.api_encryption_provisionable:
            log.warning("%s: l'Echo non supporta la cifratura: collegamento in chiaro", self.label)
            return None
        key = base64.b64encode(os.urandom(32)).decode()
        prov = api.APIClient(self.target.host, self.target.port, None, noise_psk=api.ZERO_NOISE_PSK,
                             client_info="ai-echo-dot-bridge")
        await prov.connect()
        ok = await prov.noise_encryption_set_key(key.encode())
        await prov.disconnect()
        if not ok:
            raise RuntimeError("l'Echo ha rifiutato la chiave")
        self.keys.put(self.label, key)
        log.info("%s: chiave di cifratura impostata e salvata", self.label)
        return key

    async def serve_forever(self):
        self.planner.start()
        while True:
            try:
                await self._session()
            except api.RequiresEncryptionAPIError:
                log.error("%s: l'Echo ha già una chiave che il bridge non conosce (era in Home Assistant?). "
                          "Rimuovilo da Home Assistant, oppure: adb shell rm /data/local/hassmic/state/api_key "
                          "e riavvia l'Echo", self.label)
                await asyncio.sleep(55)
            except api.InvalidEncryptionKeyAPIError:
                log.warning("%s: la chiave salvata non va più bene (reset dell'Echo?): la reimposto", self.label)
                self.keys.drop(self.label)
            except (api.APIConnectionError, OSError, asyncio.TimeoutError) as e:
                log.warning("%s: non raggiungibile (%s)", self.label, e)
            except Exception:                                     # noqa: BLE001
                log.exception("%s: errore nel collegamento", self.label)
            self.connected = False
            if self.run_task and not self.run_task.done():
                self.run_task.cancel()
            await asyncio.sleep(5)

    async def _session(self):
        key = self.keys.get(self.label) or await self._provision()
        cli = api.APIClient(self.target.host, self.target.port, None, noise_psk=key, client_info="ai-echo-dot-bridge",
                            timezone=c.TIMEZONE or None)
        stopped = asyncio.Event()

        async def on_stop(expected):
            stopped.set()

        await cli.connect(on_stop=on_stop, login=True)
        self.cli = cli
        try:
            self.info = await cli.device_info()
            self.node = self.info.name
            self.name = self.info.friendly_name or self.info.name
            ents, svcs = await cli.list_entities_services()
            self.entities = {e.key: e for e in ents}
            self.by_id = {e.object_id: e for e in ents}
            self.services = {s.name: s for s in svcs}
            self.states.clear()
            cli.subscribe_home_assistant_states_and_services(
                on_state=self._on_state, on_service_call=self._on_action,
                on_state_sub=self._on_state_sub, on_state_request=self._on_state_request)
            cli.subscribe_voice_assistant(handle_start=self._va_start, handle_stop=self._va_stop,
                                          handle_audio=self._va_audio, handle_announcement_finished=self._va_announced)
            self.connected = True
            if self.ss_music is not None and not self.ss_music.ready:
                asyncio.get_running_loop().create_task(self.ss_music.setup())
            log.info("%s: collegato a \"%s\" (%s, cifrato: %s, %d entità)", self.label, self.name, self.info.model,
                     "sì" if key else "no", len(ents))
            self.planner.on_connect()
            await stopped.wait()
            log.warning("%s: collegamento chiuso", self.label)
        finally:
            self.connected = False
            self.url_music._halt()
            self.cli = None
            try:
                await cli.disconnect()
            except Exception:                                     # noqa: BLE001
                pass

    # ------------------------------------------------------------ stati, eventi, azioni

    def _on_state(self, st):
        self.states[st.key] = st
        self.state_seq[st.key] = self.state_seq.get(st.key, 0) + 1
        ent = self.entities.get(st.key)
        if isinstance(st, api.MediaPlayerEntityState):
            self.url_music.on_media_state(st.state)
        if isinstance(st, api.model.Event) and ent is not None:
            log.info("%s: evento %s: %s", self.name, ent.object_id, st.event_type)
            self.hub.webhook({"echo": self.name, "type": "event", "entity": ent.object_id, "event": st.event_type})

    def _on_action(self, call: api.HomeassistantServiceCall):
        data = dict(call.data)
        svc = call.service
        if call.is_event and svc == "esphome.tag_scanned":
            self.hub.tag_scanned(data.get("tag_id", ""))
            return
        if svc == "assist_satellite.announce" and data.get("message"):
            log.info("%s: l'Echo chiede un annuncio: %s", self.name, data["message"])
            asyncio.get_running_loop().create_task(self.announce(data["message"]))
            return
        if svc.startswith("esphome.") and not call.is_event:
            asyncio.get_running_loop().create_task(self.hub.relay_action(self, svc[len("esphome."):], data))
            return
        log.info("%s: azione richiesta dall'Echo: %s %s", self.name, svc, data)
        self.hub.webhook({"echo": self.name, "type": "event" if call.is_event else "action", "service": svc, "data": data})

    def _on_state_sub(self, entity_id, attribute):
        self.state_subs.add((entity_id, attribute))
        self._answer_state(entity_id, attribute)

    def _on_state_request(self, entity_id, attribute):
        self._answer_state(entity_id, attribute)

    def _answer_state(self, entity_id, attribute):
        value = self.hub.ha_state(entity_id, attribute)
        if value is not None and self.cli:
            self.cli.send_home_assistant_state(entity_id, attribute, value)

    def push_state(self, entity_id, attribute, value):
        if self.cli and (entity_id, attribute) in self.state_subs:
            self.cli.send_home_assistant_state(entity_id, attribute, value)

    async def execute_action(self, action, data):
        svc = self.services.get(action)
        if not svc or not self.cli:
            return False
        typed = {}
        for arg in svc.args:
            if arg.name in data:
                v = data[arg.name]
                typed[arg.name] = {api.UserServiceArgType.BOOL: lambda x: str(x).lower() in ("1", "true", "on"),
                                   api.UserServiceArgType.INT: int, api.UserServiceArgType.FLOAT: float}.get(arg.type, str)(v)
        await self.cli.execute_service(svc, typed)
        return True

    # ------------------------------------------------------------ pipeline vocale

    def _send(self, ev, data=None):
        if self.cli:
            self.cli.send_voice_assistant_event(ev, {k: str(v) for k, v in data.items()} if data else None)

    async def _va_start(self, conversation_id, flags, audio_settings, wake_word_phrase):
        if flags & api.VoiceAssistantCommandFlag.USE_WAKE_WORD:
            log.error("%s: l'Echo chiede la wake word al server (-w remote): non supportato, togli -w remote", self.name)
            self._send(Ev.VOICE_ASSISTANT_ERROR, {"code": "wake-word-unsupported", "message": "wake word sull'Echo"})
            return None
        if self.run_task and not self.run_task.done():
            self.run_task.cancel()
        self.url_music.on_run_start()
        self._abort = False
        self.listener = speech.Listener()
        self.last_active = time.monotonic()
        self.hub.last_echo = self
        log.info("%s: richiesta (%s)", self.name, wake_word_phrase or "tasto o domanda di seguito")
        self.run_task = asyncio.get_running_loop().create_task(self._run(self.listener))
        self.run_task.add_done_callback(self._run_done)
        return 0

    def _run_done(self, task):
        if task is self.run_task:                                 # non sostituita da una richiesta più nuova
            self.url_music.on_run_end(aborted=self._abort)                                                  # 0 = audio sulla connessione API

    async def _va_stop(self, abort):
        if abort:
            self._abort = True
            if self.run_task and not self.run_task.done():
                log.info("%s: richiesta annullata dall'Echo", self.name)
                self.run_task.cancel()
        elif self.listener:
            self.listener.end()                                   # fine richiesta sentita sull'Echo

    async def _va_audio(self, data, *_):
        if self.listener:
            self.listener.feed(data)

    async def _va_announced(self, msg):
        pass                                                      # le risposte agli annunci arrivano con await

    async def _whispered(self, tonal, seq_before):
        """Sensore nativo dell'Echo (modello Amazon) se esiste; altrimenti la stima locale sul tono della voce."""
        ent = self.by_id.get("last_request_whispered")
        if ent is not None:
            for _ in range(15):                                   # l'Echo lo imposta alla fine della frase
                if self.state_seq.get(ent.key, 0) != seq_before:
                    break
                await asyncio.sleep(0.04)
            st = self.states.get(ent.key)
            if st is not None and not getattr(st, "missing_state", False) and self.state_seq.get(ent.key, 0) != seq_before:
                return bool(st.state), "sensore dell'Echo"
        if c.WHISPER_DETECT:
            return tonal < c.WHISPER_RATIO, f"parlato con tono {tonal:.0%}"
        return False, ""

    async def _run(self, listener):
        stream = None
        try:
            wh = self.by_id.get("last_request_whispered")
            seq_before = self.state_seq.get(wh.key, 0) if wh else 0
            stream = audio.AudioStream()
            self._send(Ev.VOICE_ASSISTANT_RUN_START, {"url": self.url(stream.path)})
            self._send(Ev.VOICE_ASSISTANT_STT_START)
            listener.on_speech = lambda: self._send(Ev.VOICE_ASSISTANT_STT_VAD_START)
            pcm, tonal = await listener.result()
            self.listener = None
            self._send(Ev.VOICE_ASSISTANT_STT_VAD_END)                # microfono spento, anello "penso"
            if pcm is None:
                log.info("%s: non hai detto niente", self.name)
                self._send(Ev.VOICE_ASSISTANT_ERROR, {"code": "stt-no-text-recognized", "message": "nessun parlato"})
                self._send(Ev.VOICE_ASSISTANT_RUN_END)
                return
            text_task = asyncio.get_running_loop().create_task(speech.transcribe(pcm))
            whispered, why = await self._whispered(tonal, seq_before)
            text = speech.strip_wake_word(await text_task)
            log.info("%s: hai detto: %r%s", self.name, text,
                     f" (sussurrato, {why}: risposta a {c.WHISPER_GAIN_DB:+.0f} dB)" if whispered else "")
            if not text:
                self._send(Ev.VOICE_ASSISTANT_ERROR, {"code": "stt-no-text-recognized", "message": "nessun testo"})
                self._send(Ev.VOICE_ASSISTANT_RUN_END)
                return
            self._send(Ev.VOICE_ASSISTANT_STT_END, {"text": text})
            if whispered:
                stream.gain = speech.whisper_gain()
            await self._reply(text, stream)
        except asyncio.CancelledError:
            if stream:
                stream.finish()
            raise
        except Exception as e:                                    # noqa: BLE001
            log.exception("%s: errore: %s", self.name, e)
            if stream:
                stream.finish()
            self._send(Ev.VOICE_ASSISTANT_ERROR, {"code": "bridge-error", "message": str(e)[:200]})
            self._send(Ev.VOICE_ASSISTANT_RUN_END)
        finally:
            self.listener = None

    async def _reply(self, text, stream):
        self._send(Ev.VOICE_ASSISTANT_INTENT_START)
        messages = [{"role": "user", "content": text}]
        fu, self.followup = self.followup, None
        if c.FOLLOWUP_CONTEXT and fu and time.monotonic() - fu[2] < 90:
            messages = [{"role": "user", "content": fu[0]}, {"role": "assistant", "content": fu[1]}] + messages
        said, streaming = [], False

        def go():                                                 # da qui l'Echo scarica e suona l'URL
            nonlocal streaming
            if not streaming:
                streaming = True
                self._send(Ev.VOICE_ASSISTANT_INTENT_PROGRESS, {"tts_start_streaming": "1"})

        async def keepalive():                                    # hassmic abbandona dopo 30 s in "penso"
            await asyncio.sleep(c.KEEPALIVE_S)
            if not stream.first_audio.is_set():
                log.info("%s: l'agente ci mette molto, tengo viva la richiesta", self.name)
                stream.start(22050)
                go()
                while not stream.done:
                    stream.silence(0.5)
                    await asyncio.sleep(0.5)

        ka = asyncio.get_running_loop().create_task(keepalive())
        try:
            try:
                async for sentence in speech.sentences(speech.ask_agent(messages, user=self.name)):
                    said.append(sentence)
                    log.info("%s: risposta: %s", self.name, sentence)
                    await stream.feed_sentence(sentence)
                    if not ka.done() and stream.first_audio.is_set():
                        ka.cancel()
                    go()
            except (speech.httpx.HTTPError, OSError) as e:
                log.error("%s: agente non raggiungibile: %s", self.name, e)
                ka.cancel()
                said = []
                await stream.feed_sentence(c.ERROR_PHRASE)
                go()
        finally:
            ka.cancel()
        reply = " ".join(said)
        ask_again = bool(c.FOLLOWUP and reply.rstrip().endswith("?"))
        if ask_again:
            self.followup = (text, reply, time.monotonic())
        self._send(Ev.VOICE_ASSISTANT_INTENT_END, {"conversation_id": stream.id, "continue_conversation": "1" if ask_again else "0"})
        if not stream.started:
            stream.finish()
            self._send(Ev.VOICE_ASSISTANT_ERROR, {"code": "empty-reply", "message": "risposta vuota"})
            self._send(Ev.VOICE_ASSISTANT_RUN_END)
            return
        self._send(Ev.VOICE_ASSISTANT_TTS_START, {"text": reply or c.ERROR_PHRASE})
        stream.finish()
        self._send(Ev.VOICE_ASSISTANT_TTS_END, {"url": self.url(stream.path)})
        self._send(Ev.VOICE_ASSISTANT_RUN_END)
        log.info("%s: fine risposta (%d frasi%s)", self.name, len(said), ", riascolta" if ask_again else "")

    # ------------------------------------------------------------ comandi (per MCP e API)

    async def announce(self, text, listen=False):
        """Dice un testo sull'Echo. listen=True: poi ascolta la risposta (start_conversation), che va all'agente."""
        if not self.cli:
            raise RuntimeError(f"{self.name} non è collegato")
        self._announcing += 1
        try:
            await self.url_music.before_announce()                    # l'Echo suona un URL alla volta: pausa
            stream = audio.AudioStream()
            feeder = asyncio.get_running_loop().create_task(self._feed_text(stream, text))
            try:
                res = await self.cli.send_voice_assistant_announcement_await_response(
                    self.url(stream.path), c.ANNOUNCE_TIMEOUT, text, start_conversation=listen)
            finally:
                await feeder
            log.info("%s: annuncio %s: %s", self.name, "riprodotto" if res.success else "non riprodotto", text)
            return res.success
        finally:
            self._announcing -= 1
            self.url_music.after_announce()

    async def _feed_text(self, stream, text):
        try:
            async for s in speech.sentences(_one(text)):
                await stream.feed_sentence(s)
        finally:
            stream.finish()

    def _entity(self, ref, kinds=None):
        ent = self.by_id.get(ref) or next((e for e in self.entities.values() if e.name.lower() == str(ref).lower()), None)
        if ent is None or (kinds and not isinstance(ent, kinds)):
            raise ValueError(f"entità sconosciuta su {self.name}: {ref}")
        return ent

    def media_player(self):
        return next(e for e in self.entities.values() if isinstance(e, api.MediaPlayerInfo))

    def volume(self):
        mp = next((s for s in self.states.values() if isinstance(s, api.MediaPlayerEntityState)), None)
        return round(mp.volume, 3) if mp is not None else None

    def set_volume(self, level: float):
        self.cli.media_player_command(self.media_player().key, volume=max(0.0, min(1.0, level)))

    def media(self, command=None, url=None, title="", headers=None, seek=0.0, live=False):
        """url: musica gestita dal bridge (vedi music.py). command: pause/resume(play)/stop della musica, oppure
        mute, unmute, volume_up, volume_down del media player."""
        if url:
            return self.music.play(url, title, headers, seek, live)
        cmd = (command or "").lower()
        if cmd == "pause":
            return self.music.pause()
        if cmd in ("play", "resume"):
            return self.music.resume()
        if cmd == "stop":
            self.music.stop()
            self.cli.media_player_command(self.media_player().key, command=api.MediaPlayerCommand.STOP)
            return self.music.status()
        if cmd:
            self.cli.media_player_command(self.media_player().key, command=api.MediaPlayerCommand[cmd.upper()])
        return self.music.status()

    def describe(self, entities=False):
        p = self.planner
        d = {"name": self.name, "node": self.node, "protocol": self.proto, "address": self.label,
             "connected": self.connected, "timers": p.timers_list(), "alarms": p.alarms_list(),
             "reminders": p.reminders_list(), "notifications": len(p.s["notifications"]), "night": p.night_status()}
        mp = next((s for s in self.states.values() if isinstance(s, api.MediaPlayerEntityState)), None)
        if mp is not None:
            d["volume"] = round(mp.volume, 2)
            d["media_state"] = mp.state.name if hasattr(mp.state, "name") else mp.state
        d["music"] = {**self.music.status(), "sleep": p.sleep_status()}
        if entities:
            d["entities"] = []
            for e in sorted(self.entities.values(), key=lambda e: e.object_id):
                st = self.states.get(e.key)
                item = {"id": e.object_id, "name": e.name, "type": type(e).__name__.removesuffix("Info").lower()}
                if st is not None:
                    s = _jsonable(st)
                    for k in ("key", "device_id"):
                        s.pop(k, None)
                    item["state"] = s
                for extra in ("options", "min_value", "max_value", "step", "effects", "event_types", "unit_of_measurement"):
                    if getattr(e, extra, None):
                        item[extra] = _jsonable(getattr(e, extra))
                d["entities"].append(item)
            d["actions"] = sorted(self.services)
        return d

    def command(self, ref, value=None, **kw):
        """Comando generico su un'entità: switch/number/select/button/light/update/text/fan..."""
        e = self._entity(ref)
        k, cli = e.key, self.cli
        if isinstance(e, api.SwitchInfo):
            cli.switch_command(k, str(value).lower() in ("1", "true", "on"))
        elif isinstance(e, api.NumberInfo):
            cli.number_command(k, float(value))
        elif isinstance(e, api.SelectInfo):
            cli.select_command(k, str(value))
        elif isinstance(e, api.ButtonInfo):
            cli.button_command(k)
        elif isinstance(e, api.TextInfo):
            cli.text_command(k, str(value))
        elif isinstance(e, api.UpdateInfo):
            cli.update_command(k, api.UpdateCommand[(str(value) if value else "install").upper()])
        elif isinstance(e, api.LightInfo):
            on = str(value).lower() not in ("0", "false", "off") if value is not None else True
            args = {"state": on}
            if kw.get("brightness") is not None:
                args["brightness"] = float(kw["brightness"])
            if kw.get("rgb"):
                args["rgb"] = tuple(float(x) for x in kw["rgb"])
            if kw.get("effect"):
                args["effect"] = kw["effect"]
            cli.light_command(k, **args)
        elif isinstance(e, api.MediaPlayerInfo):
            self.media(command=value)
        else:
            raise ValueError(f"{e.object_id}: tipo {type(e).__name__} non comandabile")
        return e.object_id


async def _one(text):
    yield text
