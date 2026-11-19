"""Timer gestiti dal bridge come li gestisce Home Assistant: l'Echo li mostra sull'anello e suona alla fine.

Il bridge tiene il conto del tempo e manda all'Echo gli eventi STARTED / UPDATED / CANCELLED / FINISHED. I timer
vivono in memoria: un riavvio del bridge li cancella."""
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field

from aioesphomeapi import VoiceAssistantTimerEventType as Tm

log = logging.getLogger("echo-bridge.timers")


@dataclass
class Timer:
    id: str
    name: str
    total: int
    ends_at: float = 0.0            # time.monotonic() alla fine, se attivo
    left_paused: float = 0.0        # secondi rimasti, se in pausa
    active: bool = True
    task: asyncio.Task | None = field(default=None, repr=False)

    def left(self):
        return max(0.0, self.ends_at - time.monotonic()) if self.active else self.left_paused

    def describe(self):
        return {"id": self.id, "name": self.name, "total_seconds": self.total,
                "seconds_left": round(self.left()), "active": self.active}


class Timers:
    def __init__(self, send, label):
        self._send = send           # send(event_type, timer) -> None (False se l'Echo non è collegato)
        self.label = label
        self.timers: dict[str, Timer] = {}

    def _event(self, kind, t: Timer):
        self._send(kind, t.id, t.name, t.total, round(t.left()), t.active)

    def _arm(self, t: Timer):
        if t.task:
            t.task.cancel()
        t.task = asyncio.get_running_loop().create_task(self._ring(t))

    async def _ring(self, t: Timer):
        try:
            await asyncio.sleep(t.left())
        except asyncio.CancelledError:
            return
        self.timers.pop(t.id, None)
        t.ends_at = time.monotonic()
        log.info("%s: timer finito: %s", self.label, t.name or f"{t.total} s")
        self._event(Tm.VOICE_ASSISTANT_TIMER_FINISHED, t)

    def find(self, ref: str | None):
        """Per id, per nome (senza maiuscole) o, senza riferimento, l'unico timer attivo."""
        if not ref:
            return list(self.timers.values())[:1] if len(self.timers) == 1 else []
        if ref in self.timers:
            return [self.timers[ref]]
        return [t for t in self.timers.values() if t.name.lower() == ref.lower()]

    def start(self, seconds: int, name: str = ""):
        t = Timer(id=uuid.uuid4().hex[:8], name=name or "", total=int(seconds), ends_at=time.monotonic() + seconds)
        self.timers[t.id] = t
        self._arm(t)
        self._event(Tm.VOICE_ASSISTANT_TIMER_STARTED, t)
        log.info("%s: timer avviato: %s (%d s)", self.label, name or "senza nome", seconds)
        return t

    def cancel(self, ref=None, all_=False):
        targets = list(self.timers.values()) if all_ else self.find(ref)
        for t in targets:
            if t.task:
                t.task.cancel()
            self.timers.pop(t.id, None)
            self._event(Tm.VOICE_ASSISTANT_TIMER_CANCELLED, t)
        return targets

    def pause(self, ref=None):
        out = []
        for t in self.find(ref):
            if t.active:
                t.left_paused, t.active = t.left(), False
                if t.task:
                    t.task.cancel()
                self._event(Tm.VOICE_ASSISTANT_TIMER_UPDATED, t)
                out.append(t)
        return out

    def resume(self, ref=None):
        out = []
        for t in self.find(ref):
            if not t.active:
                t.ends_at, t.active = time.monotonic() + t.left_paused, True
                self._arm(t)
                self._event(Tm.VOICE_ASSISTANT_TIMER_UPDATED, t)
                out.append(t)
        return out

    def add(self, ref, seconds):
        out = []
        for t in self.find(ref):
            if t.active:
                t.ends_at = max(time.monotonic(), t.ends_at + seconds)
                self._arm(t)
            else:
                t.left_paused = max(0.0, t.left_paused + seconds)
            t.total = max(1, t.total + seconds)
            self._event(Tm.VOICE_ASSISTANT_TIMER_UPDATED, t)
            out.append(t)
        return out

    def resend_all(self):
        """Dopo una riconnessione: l'Echo riceve di nuovo i timer ancora in corso."""
        for t in self.timers.values():
            self._event(Tm.VOICE_ASSISTANT_TIMER_STARTED, t)

    def list(self):
        return [t.describe() for t in sorted(self.timers.values(), key=lambda t: t.left())]
