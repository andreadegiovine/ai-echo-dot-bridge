"""python -m echobridge"""
import asyncio
import logging

from . import config as c
from .esphome_echo import EsphomeEcho
from .hub import Hub
from .wyoming_echo import WyomingEcho


async def main():
    logging.basicConfig(level=c.LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("aioesphomeapi").setLevel(logging.WARNING)
    log = logging.getLogger("echo-bridge")
    if not c.ECHOS:
        raise SystemExit("ECHO_HOSTS non impostato (es. ECHO_HOSTS=192.168.1.50)")
    hub = Hub()
    for t in c.ECHOS:
        hub.echos.append((EsphomeEcho if t.proto == "esphome" else WyomingEcho)(t, hub))
    log.info("Echo: %s | STT %s | TTS %s (%s) | agente %s", ", ".join(f"{t.proto}://{t.label}" for t in c.ECHOS),
             c.STT_URI, c.TTS_URI, c.TTS_VOICE, c.LLM_URL)
    tasks = [e.serve_forever() for e in hub.echos]
    if any(t.proto == "esphome" for t in c.ECHOS):
        from .server import serve
        tasks.append(serve(hub))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
