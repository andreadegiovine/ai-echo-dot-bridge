"""Tutto ciò che ha un orario, per ogni Echo (modalità ESPHome), salvato su disco in STATE_DIR/planner.json:

  timer        conto alla rovescia; l'anello lo mostra, alla fine l'Echo suona (eventi timer, come Home Assistant)
  sveglie      a un orario, anche ricorrenti; suonano con la suoneria dei timer, "Alexa, stop" le ferma
  promemoria   a un orario, anche ricorrenti; l'Echo dice il testo (annuncio). Con "non disturbare" attivo
               aspettano che finisca, come su Alexa
  notifiche    lasciate dai tool: l'anello pulsa giallo finché l'agente non le legge (read_notifications)
  modalità notte  "non disturbare" e volume più basso in una fascia oraria (NIGHT_START, NIGHT_END)
  sleep musica    la musica si abbassa piano e si ferma dopo N minuti

Un ciclo al secondo controlla le scadenze. Se l'Echo è scollegato quando qualcosa scade, viene consegnato appena si
ricollega (entro MISSED_GRACE_S); più tardi viene saltato, con una riga nel log.
"""
import asyncio
import datetime as dt
import json
import logging
import os
import time
import uuid
from zoneinfo import ZoneInfo

from aioesphomeapi import VoiceAssistantTimerEventType as Tm

from . import config as c

log = logging.getLogger("echo-bridge")
MISSED_GRACE_S = 30 * 60
DAY_NAMES = {"lun": 0, "mon": 0, "lunedi": 0, "lunedì": 0, "mar": 1, "tue": 1, "martedi": 1, "martedì": 1,
             "mer": 2, "wed": 2, "mercoledi": 2, "mercoledì": 2, "gio": 3, "thu": 3, "giovedi": 3, "giovedì": 3,
             "ven": 4, "fri": 4, "venerdi": 4, "venerdì": 4, "sab": 5, "sat": 5, "sabato": 5,
             "dom": 6, "sun": 6, "domenica": 6}
DAY_GROUPS = {"feriali": [0, 1, 2, 3, 4], "weekdays": [0, 1, 2, 3, 4], "weekend": [5, 6], "fine settimana": [5, 6],
              "tutti": list(range(7)), "ogni giorno": list(range(7)), "daily": list(range(7)), "everyday": list(range(7))}


def tz():
    try:
        return ZoneInfo(c.TIMEZONE) if c.TIMEZONE else None
    except Exception:                                             # noqa: BLE001
        return None


def now_local():
    return dt.datetime.now(tz()).astimezone(tz()) if tz() else dt.datetime.now().astimezone()


def parse_days(days):
    """["lun", "mer"], "feriali", [0, 2], "lun,mer" -> [0, 2]. Vuoto: nessuna ripetizione."""
    if not days:
        return []
    if isinstance(days, str):
        key = days.strip().lower()
        if key in DAY_GROUPS:
            return DAY_GROUPS[key]
        days = [d for d in key.replace(";", ",").split(",") if d.strip()]
    out = set()
    for d in days:
        if isinstance(d, int) or str(d).strip().isdigit():
            out.add(int(d) % 7)
        else:
            k = str(d).strip().lower()
            if k in DAY_GROUPS:
                out.update(DAY_GROUPS[k])
            elif k in DAY_NAMES or k[:3] in DAY_NAMES:
                out.add(DAY_NAMES.get(k, DAY_NAMES.get(k[:3])))
            else:
                raise ValueError(f"giorno sconosciuto: {d}")
    return sorted(out)


def parse_hhmm(s):
    h, _, m = str(s).strip().replace(".", ":").partition(":")
    h, m = int(h), int(m or 0)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"orario non valido: {s}")
    return h, m


def next_at(hhmm, days=None, date=None, after=None):
    """Prossimo istante (timestamp) per l'orario hhmm: nella data, nei giorni indicati, oppure oggi/domani."""
    h, m = parse_hhmm(hhmm)
    base = now_local() if after is None else dt.datetime.fromtimestamp(after, now_local().tzinfo)
    if date:
        d = dt.date.fromisoformat(date)
        t = dt.datetime(d.year, d.month, d.day, h, m, tzinfo=base.tzinfo)
        return t.timestamp()
    for i in range(0, 8):
        day = base.date() + dt.timedelta(days=i)
        t = dt.datetime(day.year, day.month, day.day, h, m, tzinfo=base.tzinfo)
        if t.timestamp() > base.timestamp() + 0.5 and (not days or day.weekday() in days):
            return t.timestamp()
    raise ValueError("nessun giorno valido")


def parse_at(at=None, time_=None, in_minutes=None, days=None, date=None):
    """Quando: "at" ISO ("2026-11-26T18:00"), "time" ("18:00", con days/date), oppure "in_minutes"."""
    if in_minutes not in (None, ""):
        return time.time() + float(in_minutes) * 60
    if at:
        t = dt.datetime.fromisoformat(str(at))
        if t.tzinfo is None:
            t = t.replace(tzinfo=now_local().tzinfo)
        return t.timestamp()
    if time_:
        return next_at(time_, days, date)
    raise ValueError("serve 'time', 'at' o 'in_minutes'")


def fmt(ts):
    return dt.datetime.fromtimestamp(ts, now_local().tzinfo).strftime("%Y-%m-%d %H:%M")


class Store:
    """planner.json: per ogni Echo (indirizzo) timer, sveglie, promemoria, notifiche, stato della notte."""

    def __init__(self):
        self.path = os.path.join(c.STATE_DIR, "planner.json")
        try:
            with open(self.path) as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}

    def section(self, label):
        return self.data.setdefault(label, {"timers": [], "alarms": [], "reminders": [], "notifications": [], "night": {}})

    def save(self):
        os.makedirs(c.STATE_DIR, mode=0o700, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)


def _id():
    return uuid.uuid4().hex[:8]


class Planner:
    def __init__(self, echo, store: Store):
        self.echo, self.store = echo, store
        self.s = store.section(echo.label)
        for k, v in (("timers", []), ("alarms", []), ("reminders", []), ("notifications", []), ("night", {})):
            self.s.setdefault(k, v)
        self.sleep = None                       # {"at": ts, "fade": s, "volume": v, "fading": bool}
        self.night_override = None              # "on" / "off" fino al prossimo cambio di fascia
        self._task = None
        self._busy = set()

    @property
    def name(self):
        return self.echo.name

    def save(self):
        self.store.save()

    def start(self):
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._loop())

    # ------------------------------------------------------------ collegamento

    def on_connect(self):
        """L'Echo si è (ri)collegato: timer di nuovo sull'anello, notifiche sull'anello, notte."""
        now = time.time()
        for t in self.s["timers"]:
            self._timer_event(Tm.VOICE_ASSISTANT_TIMER_STARTED, t, now)
        self._ring_notifications()

    def _send_timer(self, kind, tid, name, total, left, active):
        if self.echo.cli and self.echo.connected:
            self.echo.cli.send_voice_assistant_timer_event(kind, tid, name or "", int(total), int(max(0, left)), active)
            return True
        return False

    # ------------------------------------------------------------ timer

    def _left(self, t, now=None):
        now = now or time.time()
        return max(0.0, t["ends_at"] - now) if t["active"] else t["left"]

    def _timer_event(self, kind, t, now=None):
        return self._send_timer(kind, t["id"], t["name"], t["total"], self._left(t, now), t["active"])

    def timer_describe(self, t):
        return {"id": t["id"], "name": t["name"], "total_seconds": t["total"], "seconds_left": round(self._left(t)),
                "active": t["active"]}

    def timers_find(self, ref):
        ts = self.s["timers"]
        if not ref:
            return ts[:1] if len(ts) == 1 else []
        return [t for t in ts if t["id"] == ref or t["name"].lower() == str(ref).lower()]

    def timer_start(self, seconds, name=""):
        t = {"id": _id(), "name": name or "", "total": int(seconds), "ends_at": time.time() + int(seconds),
             "left": 0.0, "active": True}
        self.s["timers"].append(t)
        self.save()
        self._timer_event(Tm.VOICE_ASSISTANT_TIMER_STARTED, t)
        log.info("%s: timer avviato: %s (%d s)", self.name, name or "senza nome", seconds)
        return t

    def timer_cancel(self, ref=None, all_=False):
        out = list(self.s["timers"]) if all_ else self.timers_find(ref)
        for t in out:
            self.s["timers"].remove(t)
            self._timer_event(Tm.VOICE_ASSISTANT_TIMER_CANCELLED, t)
        if out:
            self.save()
        return out

    def timer_pause(self, ref=None):
        out = []
        for t in self.timers_find(ref):
            if t["active"]:
                t["left"], t["active"] = self._left(t), False
                self._timer_event(Tm.VOICE_ASSISTANT_TIMER_UPDATED, t)
                out.append(t)
        self.save()
        return out

    def timer_resume(self, ref=None):
        out = []
        for t in self.timers_find(ref):
            if not t["active"]:
                t["ends_at"], t["active"] = time.time() + t["left"], True
                self._timer_event(Tm.VOICE_ASSISTANT_TIMER_UPDATED, t)
                out.append(t)
        self.save()
        return out

    def timer_add(self, ref, seconds):
        out = []
        for t in self.timers_find(ref):
            if t["active"]:
                t["ends_at"] = max(time.time(), t["ends_at"] + seconds)
            else:
                t["left"] = max(0.0, t["left"] + seconds)
            t["total"] = max(1, t["total"] + int(seconds))
            self._timer_event(Tm.VOICE_ASSISTANT_TIMER_UPDATED, t)
            out.append(t)
        self.save()
        return out

    def timers_list(self):
        return [self.timer_describe(t) for t in sorted(self.s["timers"], key=self._left)]

    # ------------------------------------------------------------ sveglie

    def alarm_describe(self, a):
        return {"id": a["id"], "name": a["name"], "time": a["time"], "days": a["days"], "date": a.get("date"),
                "next": fmt(a["next_at"])}

    def alarm_add(self, time_, days=None, name="", date=None):
        d = parse_days(days)
        a = {"id": _id(), "name": name or "Sveglia", "time": "%02d:%02d" % parse_hhmm(time_), "days": d, "date": date,
             "next_at": next_at(time_, d, date)}
        if a["next_at"] < time.time():
            raise ValueError("la data è già passata")
        self.s["alarms"].append(a)
        self.save()
        log.info("%s: sveglia \"%s\" alle %s (prossima: %s)", self.name, a["name"], a["time"], fmt(a["next_at"]))
        return a

    def alarms_find(self, ref):
        al = self.s["alarms"]
        if not ref:
            return al[:1] if len(al) == 1 else []
        return [a for a in al if a["id"] == ref or a["name"].lower() == str(ref).lower() or a["time"] == str(ref)]

    def alarm_cancel(self, ref=None, all_=False):
        out = list(self.s["alarms"]) if all_ else self.alarms_find(ref)
        for a in out:
            self.s["alarms"].remove(a)
        if out:
            self.save()
        return out

    def alarms_list(self):
        return [self.alarm_describe(a) for a in sorted(self.s["alarms"], key=lambda a: a["next_at"])]

    def _ring_alarm(self, a):
        """Suoneria dei timer, come Alexa: suona finché non la fermi ("Alexa, stop" o tasto)."""
        return self._send_timer(Tm.VOICE_ASSISTANT_TIMER_FINISHED, "alarm-" + a["id"], a["name"], 0, 0, False)

    # ------------------------------------------------------------ promemoria

    def reminder_describe(self, r):
        return {"id": r["id"], "text": r["text"], "next": fmt(r["at"]), "time": r.get("time"), "days": r.get("days", []),
                "listen": r.get("listen", False)}

    def reminder_add(self, text, at=None, time_=None, in_minutes=None, days=None, date=None, listen=False):
        if not text:
            raise ValueError("serve il testo del promemoria")
        d = parse_days(days)
        when = parse_at(at, time_, in_minutes, d, date)
        if when < time.time() - 1:
            raise ValueError("l'orario è già passato")
        r = {"id": _id(), "text": text, "at": when, "time": "%02d:%02d" % parse_hhmm(time_) if time_ and d else None,
             "days": d if time_ else [], "listen": bool(listen)}
        self.s["reminders"].append(r)
        self.save()
        log.info("%s: promemoria per %s: %s", self.name, fmt(when), text)
        return r

    def reminders_find(self, ref):
        rs = self.s["reminders"]
        if not ref:
            return rs[:1] if len(rs) == 1 else []
        r_ = str(ref).lower()
        return [r for r in rs if r["id"] == ref or r_ in r["text"].lower()]

    def reminder_cancel(self, ref=None, all_=False):
        out = list(self.s["reminders"]) if all_ else self.reminders_find(ref)
        for r in out:
            self.s["reminders"].remove(r)
        if out:
            self.save()
        return out

    def reminders_list(self):
        return [self.reminder_describe(r) for r in sorted(self.s["reminders"], key=lambda r: r["at"])]

    async def _deliver_reminder(self, r):
        try:
            ok = await self.echo.announce(f"Promemoria: {r['text']}", listen=r.get("listen", False))
        except Exception as e:                                    # noqa: BLE001
            log.warning("%s: promemoria non consegnato (%s), riprovo", self.name, e)
            ok = False
        if not ok:
            r["retry_at"] = time.time() + 60
            self.save()
            return
        log.info("%s: promemoria detto: %s", self.name, r["text"])
        self._reschedule_reminder(r)

    def _reschedule_reminder(self, r):
        r.pop("retry_at", None)
        if r.get("time") and r.get("days"):
            r["at"] = next_at(r["time"], r["days"])
        elif r in self.s["reminders"]:
            self.s["reminders"].remove(r)
        self.save()

    # ------------------------------------------------------------ notifiche (anello giallo)

    def notify(self, text, source=""):
        n = {"id": _id(), "text": text, "source": source, "at": time.time()}
        self.s["notifications"].append(n)
        self.save()
        log.info("%s: notifica%s: %s", self.name, f" da {source}" if source else "", text)
        self._ring_notifications()
        return n

    def notifications_list(self):
        return [{"id": n["id"], "text": n["text"], "source": n.get("source", ""), "at": fmt(n["at"])}
                for n in self.s["notifications"]]

    def notifications_clear(self, ref=None):
        ns = self.s["notifications"]
        out = list(ns) if not ref else [n for n in ns if n["id"] == ref]
        for n in out:
            ns.remove(n)
        self.save()
        self._ring_notifications()
        return out

    def _ring_notifications(self):
        ring = self.echo.by_id.get("light_ring") if self.echo.connected else None
        if ring is None or not self.echo.cli:
            return
        if self.s["notifications"]:
            effect = next((e for e in getattr(ring, "effects", []) or [] if e.lower() == "pulse"), None)
            args = {"state": True, "rgb": (1.0, 0.65, 0.0), "brightness": 0.8}
            if effect:
                args["effect"] = effect
            self.echo.cli.light_command(ring.key, **args)
        else:
            self.echo.cli.light_command(ring.key, state=False)

    # ------------------------------------------------------------ modalità notte

    def night_window(self):
        if not (c.NIGHT_START and c.NIGHT_END):
            return False
        n = now_local()
        cur = n.hour * 60 + n.minute
        a, b = (h * 60 + m for h, m in (parse_hhmm(c.NIGHT_START), parse_hhmm(c.NIGHT_END)))
        return (a <= cur < b) if a < b else (cur >= a or cur < b)

    def night_wanted(self):
        return self.night_override == "on" if self.night_override else self.night_window()

    def night_set(self, mode):
        """mode: on / off (fino al prossimo cambio di fascia) / auto."""
        if mode not in ("on", "off", "auto"):
            raise ValueError("modalità: on, off o auto")
        self.night_override = None if mode == "auto" else mode
        self.s["night"]["window_was"] = self.night_window()
        self._apply_night()
        return self.night_status()

    def night_status(self):
        return {"active": bool(self.s["night"].get("active")), "mode": self.night_override or "auto",
                "window": f"{c.NIGHT_START}-{c.NIGHT_END}" if c.NIGHT_START and c.NIGHT_END else None,
                "dnd": c.NIGHT_DND, "volume": c.NIGHT_VOLUME}

    def _apply_night(self):
        night = self.s["night"]
        want = self.night_wanted()
        if not self.echo.connected or bool(night.get("active")) == want:
            return
        dnd = self.echo.by_id.get("do_not_disturb")
        if want:
            if c.NIGHT_DND and dnd is not None:
                self.echo.cli.switch_command(dnd.key, True)
            if c.NIGHT_VOLUME is not None:
                night["saved_volume"] = self.echo.volume()
                self.echo.set_volume(c.NIGHT_VOLUME / 100)
            log.info("%s: modalità notte attiva", self.name)
        else:
            if c.NIGHT_DND and dnd is not None:
                self.echo.cli.switch_command(dnd.key, False)
            if c.NIGHT_VOLUME is not None and night.get("saved_volume") is not None:
                self.echo.set_volume(night["saved_volume"])
            log.info("%s: modalità notte finita", self.name)
        night["active"] = want
        self.save()

    # ------------------------------------------------------------ sleep della musica

    def music_sleep(self, minutes, fade=30):
        if minutes is None or float(minutes) <= 0:
            self.sleep = None
            log.info("%s: sleep della musica annullato", self.name)
            return None
        self.sleep = {"at": time.time() + float(minutes) * 60, "fade": max(0.0, float(fade)), "volume": None}
        log.info("%s: la musica si ferma alle %s", self.name, fmt(self.sleep["at"]))
        return self.sleep_status()

    def sleep_status(self):
        if not self.sleep:
            return None
        return {"stops_at": fmt(self.sleep["at"]), "seconds_left": round(max(0, self.sleep["at"] - time.time())),
                "fade_seconds": self.sleep["fade"]}

    def _tick_sleep(self, now):
        sl = self.sleep
        if not sl:
            return
        if self.echo.music.status().get("state") != "playing" and sl["volume"] is None:
            if now >= sl["at"]:
                self.sleep = None                                 # già ferma: niente da fare
            return
        start_fade = sl["at"] - sl["fade"]
        if now < start_fade:
            return
        if sl["volume"] is None:
            sl["volume"] = self.echo.volume() or 0.5
            log.info("%s: sleep: la musica si abbassa", self.name)
        if now < sl["at"] and sl["fade"] > 0:
            self.echo.set_volume(sl["volume"] * max(0.0, (sl["at"] - now) / sl["fade"]))
            return
        self.echo.media(command="stop")
        self.echo.set_volume(sl["volume"])
        log.info("%s: sleep: musica fermata, volume di nuovo a %d", self.name, round(sl["volume"] * 100))
        self.sleep = None

    # ------------------------------------------------------------ ciclo

    def _spawn(self, key, coro):
        if key in self._busy:
            coro.close()
            return
        self._busy.add(key)
        t = asyncio.get_running_loop().create_task(coro)
        t.add_done_callback(lambda _t: self._busy.discard(key))

    def dnd_on(self):
        dnd = self.echo.by_id.get("do_not_disturb")
        st = self.echo.states.get(dnd.key) if dnd is not None else None
        return bool(getattr(st, "state", False))

    async def _loop(self):
        while True:
            try:
                self.tick()
            except Exception:                                     # noqa: BLE001
                log.exception("%s: errore nel pianificatore", self.name)
            await asyncio.sleep(1)

    def tick(self, now=None):
        now = now or time.time()
        connected = self.echo.connected and self.echo.cli is not None
        changed = False
        # timer
        for t in list(self.s["timers"]):
            if t["active"] and t["ends_at"] <= now:
                if connected:
                    self.s["timers"].remove(t)
                    t["active"] = False
                    self._send_timer(Tm.VOICE_ASSISTANT_TIMER_FINISHED, t["id"], t["name"], t["total"], 0, False)
                    log.info("%s: timer finito: %s", self.name, t["name"] or f"{t['total']} s")
                    changed = True
                elif now - t["ends_at"] > MISSED_GRACE_S:
                    self.s["timers"].remove(t)
                    log.warning("%s: timer %s finito mentre l'Echo era scollegato: saltato", self.name, t["name"])
                    changed = True
        # sveglie
        for a in list(self.s["alarms"]):
            if a["next_at"] > now:
                continue
            if connected:
                self._ring_alarm(a)
                log.info("%s: sveglia \"%s\" (%s)", self.name, a["name"], a["time"])
            elif now - a["next_at"] <= MISSED_GRACE_S:
                continue                                          # aspetta che l'Echo torni
            else:
                log.warning("%s: sveglia \"%s\" delle %s persa: Echo scollegato", self.name, a["name"], a["time"])
            if a["days"]:
                a["next_at"] = next_at(a["time"], a["days"], after=max(now, a["next_at"]))
            else:
                self.s["alarms"].remove(a)
            changed = True
        # promemoria
        for r in list(self.s["reminders"]):
            due = r.get("retry_at", r["at"])
            if due > now:
                continue
            if now - r["at"] > MISSED_GRACE_S and not connected:
                log.warning("%s: promemoria perso (Echo scollegato): %s", self.name, r["text"])
                self._reschedule_reminder(r)
                continue
            if not connected:
                continue
            if self.dnd_on():
                continue                                          # come Alexa: dopo "non disturbare"
            self._spawn("rem-" + r["id"], self._deliver_reminder(r))
        # notte e sleep
        if connected:
            night = self.s["night"]
            win = self.night_window()
            if self.night_override and night.get("window_was") is not None and night["window_was"] != win:
                self.night_override = None                        # la fascia è cambiata: torna automatica
            night["window_was"] = win
            self._apply_night()
            self._tick_sleep(now)
        if changed:
            self.save()
