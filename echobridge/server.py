"""Server HTTP del bridge: audio per l'Echo, strumenti MCP per l'agente, API REST per tool e automazioni.
Tutte le richieste in ingresso sono descritte in REQUESTS.md."""
import asyncio
import json
import logging

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from . import audio, config as c
from .hub import Hub

log = logging.getLogger("echo-bridge")


# ---------------------------------------------------------------- operazioni (le stesse per REST e MCP)

def _one_or_all(hub, ref, fn):
    """Esegue fn(echo) sull'Echo indicato, o su tutti con echo = "all"/"tutti"."""
    targets = hub.pick_many(ref)
    results = [{"echo": e.name, **fn(e)} for e in targets]
    return results[0] if len(results) == 1 and not hub.is_all(ref) else {"all": True, "results": results}


def op_timers(hub, b):
    action, name = b.get("action", "start"), b.get("name") or None

    def run(e):
        p = e.planner
        if action == "start":
            out = [p.timer_start(int(b["seconds"]), b.get("name", ""))]
        elif action == "cancel":
            out = p.timer_cancel(name, all_=bool(b.get("all")))
        elif action == "pause":
            out = p.timer_pause(name)
        elif action == "resume":
            out = p.timer_resume(name)
        elif action == "add":
            out = p.timer_add(name, int(b["seconds"]))
        elif action == "list":
            return {"timers": p.timers_list()}
        else:
            raise ValueError(f"azione sconosciuta: {action}")
        return {"timers": [p.timer_describe(t) for t in out]}
    return _one_or_all(hub, b.get("echo"), run)


def op_alarms(hub, b):
    action = b.get("action", "set")

    def run(e):
        p = e.planner
        if action == "set":
            return {"alarms": [p.alarm_describe(p.alarm_add(b["time"], b.get("days"), b.get("name", ""), b.get("date")))]}
        if action == "cancel":
            return {"alarms": [p.alarm_describe(a) for a in p.alarm_cancel(b.get("name") or b.get("id"), all_=bool(b.get("all")))]}
        if action == "list":
            return {"alarms": p.alarms_list()}
        raise ValueError(f"azione sconosciuta: {action}")
    return _one_or_all(hub, b.get("echo"), run)


def op_reminders(hub, b):
    action = b.get("action", "set")

    def run(e):
        p = e.planner
        if action == "set":
            r = p.reminder_add(b.get("text", ""), b.get("at"), b.get("time"), b.get("in_minutes"), b.get("days"),
                               b.get("date"), bool(b.get("listen")))
            return {"reminders": [p.reminder_describe(r)]}
        if action == "cancel":
            return {"reminders": [p.reminder_describe(r) for r in p.reminder_cancel(b.get("id") or b.get("text"),
                                                                                    all_=bool(b.get("all")))]}
        if action == "list":
            return {"reminders": p.reminders_list()}
        raise ValueError(f"azione sconosciuta: {action}")
    return _one_or_all(hub, b.get("echo"), run)


def op_notify(hub, b):
    action = b.get("action", "add")

    def run(e):
        p = e.planner
        if action == "add":
            if not b.get("text"):
                raise ValueError("serve il testo della notifica")
            p.notify(b["text"], b.get("source", ""))
            return {"notifications": p.notifications_list()}
        if action == "list":
            return {"notifications": p.notifications_list()}
        if action in ("read", "clear"):
            out = p.notifications_list()
            p.notifications_clear(b.get("id"))
            return {"notifications": out if not b.get("id") else [n for n in out if n["id"] == b["id"]], "cleared": True}
        raise ValueError(f"azione sconosciuta: {action}")
    return _one_or_all(hub, b.get("echo"), run)


def op_music(hub, b):
    action = b.get("action", "play" if b.get("url") else "status")

    def run(e):
        if action == "play":
            st = e.music.play(b["url"], b.get("title", ""), b.get("headers"), float(b.get("seek", 0)), bool(b.get("live")))
        elif action in ("pause", "resume", "stop"):
            st = e.media(command=action)
        elif action == "sleep":
            e.planner.music_sleep(b.get("minutes"), b.get("fade", 30))
            st = e.music.status()
        elif action == "status":
            st = e.music.status()
        else:
            raise ValueError(f"azione sconosciuta: {action}")
        return {**st, "sleep": e.planner.sleep_status()}
    return _one_or_all(hub, b.get("echo"), run)


def op_night(hub, b):
    action = b.get("action", "status")

    def run(e):
        return e.planner.night_status() if action == "status" else e.planner.night_set(action)
    return _one_or_all(hub, b.get("echo"), run)


async def op_announce(hub, b):
    if not b.get("text"):
        raise ValueError("serve il testo dell'annuncio")
    targets = hub.pick_many(b.get("echo"))
    played = await asyncio.gather(*(e.announce(b["text"], listen=bool(b.get("listen"))) for e in targets),
                                  return_exceptions=True)
    results = [{"echo": e.name, "played": p is True} for e, p in zip(targets, played)]
    return results[0] if len(results) == 1 and not hub.is_all(b.get("echo")) else {"all": True, "results": results}


# ---------------------------------------------------------------- server

def build(hub: Hub):
    mcp = MCPServer(
        "echo-bridge",
        instructions=(
            "Strumenti per gli Echo Dot di casa (altoparlanti vocali). Senza il parametro echo si usa l'Echo che ha "
            "ricevuto l'ultima richiesta vocale, cioè quello da cui l'utente ti sta parlando; echo=\"all\" vale per "
            "tutti. Timer (set_timer) e sveglie (set_alarm) suonano sull'Echo; i promemoria (set_reminder) li dice a "
            "voce. Se l'anello è giallo ci sono notifiche: leggile con read_notifications."))

    @mcp.tool(description="Elenca gli Echo con stato, volume, musica, timer, sveglie, promemoria e notifiche.")
    async def list_echos() -> dict:
        return {"echos": [e.describe() for e in hub.echos]}

    @mcp.tool(description="Stato completo di un Echo: entità (impostazioni, sensori, luce), valori attuali e azioni.")
    async def echo_status(echo: str = "") -> dict:
        return hub.pick(echo or None).describe(entities=True)

    @mcp.tool(description="Fa dire un testo a un Echo (echo=\"all\": a tutti). listen=true: dopo l'annuncio l'Echo "
                          "ascolta la risposta dell'utente e la manda all'agente come una normale richiesta.")
    async def announce(text: str, echo: str = "", listen: bool = False) -> dict:
        return await op_announce(hub, {"text": text, "echo": echo, "listen": listen})

    @mcp.tool(description="Avvia un timer. seconds: durata in secondi; name: facoltativo (es. 'pasta'). "
                          "Alla fine l'Echo suona finché non lo fermano.")
    async def set_timer(seconds: int, name: str = "", echo: str = "") -> dict:
        return op_timers(hub, {"action": "start", "seconds": seconds, "name": name, "echo": echo})

    @mcp.tool(description="Elenca i timer in corso.")
    async def list_timers(echo: str = "") -> dict:
        return op_timers(hub, {"action": "list", "echo": echo})

    @mcp.tool(description="Annulla un timer per nome o id; senza nome l'unico timer; all=true tutti.")
    async def cancel_timer(name: str = "", all: bool = False, echo: str = "") -> dict:
        return op_timers(hub, {"action": "cancel", "name": name, "all": all, "echo": echo})

    @mcp.tool(description="Mette in pausa (pause=true) o riprende (pause=false) un timer.")
    async def pause_timer(name: str = "", pause: bool = True, echo: str = "") -> dict:
        return op_timers(hub, {"action": "pause" if pause else "resume", "name": name, "echo": echo})

    @mcp.tool(description="Aggiunge (seconds > 0) o toglie (seconds < 0) tempo a un timer.")
    async def change_timer(seconds: int, name: str = "", echo: str = "") -> dict:
        return op_timers(hub, {"action": "add", "seconds": seconds, "name": name, "echo": echo})

    @mcp.tool(description="Sveglia a un orario (time \"07:00\"). days per ripeterla: \"feriali\", \"weekend\", "
                          "\"tutti\" o giorni (\"lun,mer,ven\"); date \"AAAA-MM-GG\" per un giorno preciso; senza: la "
                          "prossima volta che è quell'ora. Suona come un timer finché non la fermano.")
    async def set_alarm(time: str, days: str = "", name: str = "", date: str = "", echo: str = "") -> dict:
        return op_alarms(hub, {"action": "set", "time": time, "days": days, "name": name, "date": date or None, "echo": echo})

    @mcp.tool(description="Elenca le sveglie.")
    async def list_alarms(echo: str = "") -> dict:
        return op_alarms(hub, {"action": "list", "echo": echo})

    @mcp.tool(description="Cancella una sveglia per nome, orario (\"07:00\") o id; all=true tutte.")
    async def cancel_alarm(name: str = "", all: bool = False, echo: str = "") -> dict:
        return op_alarms(hub, {"action": "cancel", "name": name, "all": all, "echo": echo})

    @mcp.tool(description="Promemoria detto a voce dall'Echo. Quando: time \"18:00\" (con days per ripeterlo, come "
                          "le sveglie, o date), oppure at (\"2026-11-26T18:00\"), oppure in_minutes. listen=true: dopo "
                          "il promemoria l'Echo ascolta la risposta. Con \"non disturbare\" attivo aspetta che finisca.")
    async def set_reminder(text: str, time: str = "", at: str = "", in_minutes: float | None = None, days: str = "",
                           date: str = "", listen: bool = False, echo: str = "") -> dict:
        return op_reminders(hub, {"action": "set", "text": text, "time": time or None, "at": at or None,
                                  "in_minutes": in_minutes, "days": days, "date": date or None, "listen": listen, "echo": echo})

    @mcp.tool(description="Elenca i promemoria.")
    async def list_reminders(echo: str = "") -> dict:
        return op_reminders(hub, {"action": "list", "echo": echo})

    @mcp.tool(description="Cancella un promemoria per id o per una parola del testo; all=true tutti.")
    async def cancel_reminder(text_or_id: str = "", all: bool = False, echo: str = "") -> dict:
        return op_reminders(hub, {"action": "cancel", "text": text_or_id, "all": all, "echo": echo})

    @mcp.tool(description="Lascia una notifica sull'Echo senza interrompere: l'anello pulsa giallo finché non viene "
                          "letta. Per cose non urgenti.")
    async def notify(text: str, source: str = "", echo: str = "") -> dict:
        return op_notify(hub, {"action": "add", "text": text, "source": source, "echo": echo})

    @mcp.tool(description="Legge le notifiche in attesa (\"Alexa, che notifiche ho?\") e, con clear=true (default), le "
                          "segna come lette e spegne l'anello giallo. Riferiscile all'utente nella risposta.")
    async def read_notifications(clear: bool = True, echo: str = "") -> dict:
        return op_notify(hub, {"action": "read" if clear else "list", "echo": echo})

    @mcp.tool(description="Imposta il volume dell'Echo, da 0 a 100.")
    async def set_volume(level: int, echo: str = "") -> dict:
        return _one_or_all(hub, echo, lambda e: (e.set_volume(level / 100), {"volume": level})[1])

    @mcp.tool(description="Musica sull'Echo. url: audio da riprodurre (anche da internet; per YouTube l'URL audio "
                          "diretto con i suoi headers). title: titolo. seek: da quale secondo. live=true per radio. "
                          "Senza url, command = pause, resume, stop, status, mute, unmute, volume_up, volume_down. "
                          "Quando l'utente parla o c'è un annuncio la musica si abbassa da sola.")
    async def media(command: str = "", url: str = "", title: str = "", headers: dict | None = None,
                    seek: float = 0.0, live: bool = False, echo: str = "") -> dict:
        if url:
            return op_music(hub, {"action": "play", "url": url, "title": title, "headers": headers, "seek": seek,
                                  "live": live, "echo": echo})
        if command in ("", "status", "pause", "resume", "stop"):
            return op_music(hub, {"action": command or "status", "echo": echo})
        return _one_or_all(hub, echo, lambda e: e.media(command=command))

    @mcp.tool(description="Ferma la musica tra N minuti, abbassandola piano negli ultimi fade secondi (\"spegni la "
                          "musica tra mezz'ora\"). minutes=0 annulla.")
    async def music_sleep(minutes: float, fade: float = 30, echo: str = "") -> dict:
        return op_music(hub, {"action": "sleep", "minutes": minutes, "fade": fade, "echo": echo})

    @mcp.tool(description="Modalità notte (non disturbare e volume basso): mode = on, off (fino al prossimo cambio "
                          "di fascia oraria), auto, status.")
    async def night_mode(mode: str = "status", echo: str = "") -> dict:
        return op_night(hub, {"action": mode, "echo": echo})

    @mcp.tool(description="Comanda un'entità dell'Echo (vedi echo_status): interruttori (value on/off), numeri, "
                          "selettori, pulsanti, aggiornamento (value install), luce dell'anello (value on/off, "
                          "brightness 0-1, rgb [r,g,b] 0-1, effect).")
    async def set_entity(entity: str, value: str = "", brightness: float | None = None,
                         rgb: list[float] | None = None, effect: str = "", echo: str = "") -> str:
        e = hub.pick_esphome(echo or None)
        oid = e.command(entity, value if value != "" else None, brightness=brightness, rgb=rgb, effect=effect or None)
        return f"{e.name}: {oid} comandato"

    @mcp.tool(description="Esegue un'azione dell'Echo (vedi 'actions' in echo_status), es. drop_in con "
                          "data {\"target\": \"Cucina\"} per chiamare un altro Echo.")
    async def run_action(action: str, data: dict | None = None, echo: str = "") -> str:
        e = hub.pick_esphome(echo or None)
        ok = await e.execute_action(action, data or {})
        return "eseguita" if ok else f"azione sconosciuta: {action}"

    # ------------------------------------------------------------ HTTP

    @mcp.custom_route("/audio/{name}", methods=["GET"])
    async def audio_route(request: Request) -> Response:
        s = audio.get(request.path_params["name"].split(".")[0])
        if s is None:
            return Response(status_code=404)
        return StreamingResponse(s.read(), media_type=getattr(s, "media_type", "audio/wav"))

    @mcp.custom_route("/healthz", methods=["GET"])
    async def health(request: Request) -> Response:
        return JSONResponse({"ok": True, "echos": [{"name": e.name, "connected": e.connected} for e in hub.echos]})

    @mcp.custom_route("/api/echos", methods=["GET"])
    async def api_echos(request: Request) -> Response:
        return JSONResponse([e.describe(entities=request.query_params.get("entities") == "1") for e in hub.echos])

    def rest(path, op, default_get="list", is_async=False):
        async def handler(request: Request) -> Response:
            try:
                if request.method == "GET":
                    body = {"action": default_get, **dict(request.query_params)}
                else:
                    body = await request.json()
                    if not isinstance(body, dict):
                        raise ValueError("serve un oggetto JSON")
                out = await op(hub, body) if is_async else op(hub, body)
            except (ValueError, KeyError, RuntimeError, TypeError) as ex:
                msg = f"campo mancante: {ex}" if isinstance(ex, KeyError) else str(ex)
                return JSONResponse({"error": msg}, status_code=400)
            return JSONResponse(out)
        mcp.custom_route(path, methods=["POST"] if is_async else ["GET", "POST"])(handler)

    rest("/api/announce", op_announce, is_async=True)
    rest("/api/timers", op_timers)
    rest("/api/alarms", op_alarms)
    rest("/api/reminders", op_reminders)
    rest("/api/notify", op_notify)
    rest("/api/music", op_music, default_get="status")
    rest("/api/night", op_night, default_get="status")

    app = mcp.streamable_http_app(
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False), host=c.HTTP_HOST)
    return _auth(app) if c.API_TOKEN else app


def _auth(app):
    """Con API_TOKEN: /mcp e /api vogliono "Authorization: Bearer <token>". /audio resta libero (lo scarica l'Echo)."""
    want = f"Bearer {c.API_TOKEN}".encode()

    async def wrapped(scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith(("/mcp", "/api")):
            if dict(scope.get("headers") or []).get(b"authorization") != want:
                await Response(json.dumps({"error": "unauthorized"}), status_code=401,
                               media_type="application/json")(scope, receive, send)
                return
        await app(scope, receive, send)
    return wrapped


async def serve(hub: Hub):
    config = uvicorn.Config(build(hub), host=c.HTTP_HOST, port=c.HTTP_PORT, log_level="warning", lifespan="on")
    log.info("server HTTP su %s:%d (audio, MCP su /mcp, API su /api)", c.HTTP_HOST, c.HTTP_PORT)
    await uvicorn.Server(config).serve()
