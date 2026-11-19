"""Server HTTP del bridge: audio per l'Echo, strumenti MCP per l'agente, piccola API REST.

  GET  /audio/<id>.wav     risposte e annunci in streaming (li scarica l'Echo)
  POST /mcp                server MCP (streamable HTTP): l'agente comanda gli Echo con i suoi strumenti
  GET  /api/echos          stato degli Echo
  POST /api/announce       {"text": "...", "echo": "...", "listen": false}
  GET  /healthz
"""
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


def build(hub: Hub):
    mcp = MCPServer(
        "echo-bridge",
        instructions=(
            "Strumenti per gli Echo Dot di casa (altoparlanti vocali). Senza il parametro echo si usa l'Echo che ha "
            "ricevuto l'ultima richiesta vocale, cioè quello da cui l'utente ti sta parlando. Usa set_timer per i "
            "timer (l'Echo suona alla fine), announce per far dire qualcosa a un Echo."))

    @mcp.tool(description="Elenca gli Echo con stato, volume e timer in corso.")
    async def list_echos() -> dict:
        return {"echos": [e.describe() for e in hub.echos]}

    @mcp.tool(description="Stato completo di un Echo: entità (impostazioni, sensori, luce), valori attuali e azioni.")
    async def echo_status(echo: str = "") -> dict:
        return hub.pick(echo or None).describe(entities=True)

    @mcp.tool(description="Fa dire un testo a un Echo. listen=true: dopo l'annuncio l'Echo ascolta la risposta "
                          "dell'utente e la manda all'agente come una normale richiesta.")
    async def announce(text: str, echo: str = "", listen: bool = False) -> str:
        e = hub.pick_esphome(echo or None)
        ok = await e.announce(text, listen=listen)
        return f"annuncio su {e.name}: {'riprodotto' if ok else 'non riprodotto (non disturbare o errore)'}"

    @mcp.tool(description="Avvia un timer sull'Echo. seconds: durata totale in secondi; name: nome facoltativo "
                          "(es. 'pasta'). Alla fine l'Echo suona finché qualcuno non lo ferma.")
    async def set_timer(seconds: int, name: str = "", echo: str = "") -> dict:
        e = hub.pick_esphome(echo or None)
        return e.timers.start(seconds, name).describe()

    @mcp.tool(description="Elenca i timer in corso su un Echo.")
    async def list_timers(echo: str = "") -> dict:
        return {"timers": hub.pick_esphome(echo or None).timers.list()}

    @mcp.tool(description="Annulla un timer per nome o id; senza nome annulla l'unico timer, all=true li annulla tutti.")
    async def cancel_timer(name: str = "", all: bool = False, echo: str = "") -> dict:
        return {"cancelled": [t.describe() for t in hub.pick_esphome(echo or None).timers.cancel(name or None, all_=all)]}

    @mcp.tool(description="Mette in pausa (pause=true) o riprende (pause=false) un timer.")
    async def pause_timer(name: str = "", pause: bool = True, echo: str = "") -> dict:
        t = hub.pick_esphome(echo or None).timers
        return {"timers": [x.describe() for x in (t.pause(name or None) if pause else t.resume(name or None))]}

    @mcp.tool(description="Aggiunge (seconds > 0) o toglie (seconds < 0) tempo a un timer.")
    async def change_timer(seconds: int, name: str = "", echo: str = "") -> dict:
        return {"timers": [x.describe() for x in hub.pick_esphome(echo or None).timers.add(name or None, seconds)]}

    @mcp.tool(description="Imposta il volume dell'Echo, da 0 a 100.")
    async def set_volume(level: int, echo: str = "") -> str:
        e = hub.pick_esphome(echo or None)
        e.set_volume(level / 100)
        return f"volume di {e.name} a {level}"

    @mcp.tool(description="Riproduce un URL audio (http, sulla rete locale) o comanda la riproduzione: "
                          "command = play, pause, stop, mute, unmute, volume_up, volume_down.")
    async def media(command: str = "", url: str = "", echo: str = "") -> str:
        e = hub.pick_esphome(echo or None)
        e.media(command=command or None, url=url or None)
        return "ok"

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

    @mcp.custom_route("/audio/{name}", methods=["GET"])
    async def audio_route(request: Request) -> Response:
        s = audio.get(request.path_params["name"].removesuffix(".wav"))
        if s is None:
            return Response(status_code=404)
        return StreamingResponse(s.read(), media_type="audio/wav")

    @mcp.custom_route("/healthz", methods=["GET"])
    async def health(request: Request) -> Response:
        return JSONResponse({"ok": True, "echos": [{"name": e.name, "connected": e.connected} for e in hub.echos]})

    @mcp.custom_route("/api/echos", methods=["GET"])
    async def api_echos(request: Request) -> Response:
        return JSONResponse([e.describe(entities=request.query_params.get("entities") == "1") for e in hub.echos])

    @mcp.custom_route("/api/announce", methods=["POST"])
    async def api_announce(request: Request) -> Response:
        body = await request.json()
        try:
            e = hub.pick_esphome(body.get("echo") or None)
            ok = await e.announce(body["text"], listen=bool(body.get("listen")))
        except (ValueError, KeyError, RuntimeError) as ex:
            return JSONResponse({"error": str(ex)}, status_code=400)
        return JSONResponse({"echo": e.name, "played": ok})

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
