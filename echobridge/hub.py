"""Gli Echo collegati e ciò che hanno in comune: tag di arbitraggio, azioni tra Echo, webhook, scelta dell'Echo."""
import asyncio
import datetime
import logging

import httpx

from . import config as c

log = logging.getLogger("echo-bridge")


class Hub:
    def __init__(self):
        self.echos = []
        self.last_echo = None
        self.tags: dict[str, str] = {}              # tag_id -> ultima scansione (come tag.<id> in Home Assistant)
        self._bg = set()

    # ------------------------------------------------------------ scelta dell'Echo

    def pick(self, ref=None):
        """Per nome, nome del nodo o indirizzo; senza riferimento: l'ultimo che ha ricevuto una richiesta."""
        if ref:
            r = str(ref).lower()
            for e in self.echos:
                if r in {str(e.name).lower(), str(getattr(e, "node", "") or "").lower(), e.label.lower(), e.target.host}:
                    return e
            raise ValueError(f"Echo sconosciuto: {ref}. Disponibili: {', '.join(e.name for e in self.echos)}")
        if self.last_echo is not None:
            return self.last_echo
        if self.echos:
            return self.echos[0]
        raise ValueError("nessun Echo configurato")

    def pick_esphome(self, ref=None):
        e = self.pick(ref)
        if e.proto != "esphome":
            raise ValueError(f"{e.name} è in modalità Wyoming: serve la modalità ESPHome per questa funzione")
        if not e.connected:
            raise ValueError(f"{e.name} non è collegato")
        return e

    # ------------------------------------------------------------ ciò che Home Assistant risponderebbe

    def ha_state(self, entity_id, attribute):
        if entity_id == "zone.home" and attribute == "latitude" and c.HOME_LATITUDE:
            return c.HOME_LATITUDE
        if entity_id == "zone.home" and attribute == "longitude" and c.HOME_LONGITUDE:
            return c.HOME_LONGITUDE
        if entity_id.startswith("tag.") and not attribute:
            return self.tags.get(entity_id[4:])
        return None

    def tag_scanned(self, tag_id):
        if not tag_id:
            return
        when = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.tags[tag_id] = when
        log.debug("tag %s scansionato", tag_id)
        for e in self.echos:
            if e.proto == "esphome":
                e.push_state(f"tag.{tag_id}", None, when)

    async def relay_action(self, source, name, data):
        """Un Echo chiede di eseguire l'azione "esphome.<nodo>_<azione>" di un altro (es. arbitration_key)."""
        for e in self.echos:
            node = (getattr(e, "node", None) or "").replace("-", "_")
            if e.proto == "esphome" and node and name.startswith(node + "_"):
                action = name[len(node) + 1:]
                try:
                    ok = await e.execute_action(action, data)
                    log.info("%s -> %s: azione %s %s", source.name, e.name, action, "eseguita" if ok else "sconosciuta")
                except Exception as ex:                           # noqa: BLE001
                    log.warning("%s -> %s: azione %s fallita: %s", source.name, e.name, action, ex)
                return
        log.info("%s: azione esphome.%s per un dispositivo che il bridge non conosce", source.name, name)

    # ------------------------------------------------------------ webhook

    def webhook(self, payload):
        if not c.EVENT_WEBHOOK:
            return
        t = asyncio.get_running_loop().create_task(self._post(payload))
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def _post(self, payload):
        try:
            async with httpx.AsyncClient(timeout=5) as http:
                await http.post(c.EVENT_WEBHOOK, json=payload)
        except httpx.HTTPError as e:
            log.warning("webhook non raggiungibile: %s", e)
