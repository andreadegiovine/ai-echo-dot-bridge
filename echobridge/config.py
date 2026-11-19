"""Configurazione da variabili d'ambiente (vedi echo-bridge.env)."""
import json
import os
from dataclasses import dataclass


def env(name, default=None, cast=str):
    v = os.environ.get(name)
    return default if v in (None, "") else cast(v)


def flag(name, default):
    return env(name, "1" if default else "0") in ("1", "true", "yes", "on")


@dataclass
class EchoTarget:
    proto: str          # "esphome" | "wyoming"
    host: str
    port: int

    @property
    def label(self):
        return f"{self.host}:{self.port}"


def parse_echos(spec: str, default_proto: str):
    """"192.168.1.50, wyoming://192.168.1.51, esphome://192.168.1.52:26053" -> [EchoTarget]"""
    out = []
    for item in (x.strip() for x in spec.split(",")):
        if not item:
            continue
        proto = default_proto
        if "://" in item:
            proto, item = item.split("://", 1)
        host, _, port = item.partition(":")
        out.append(EchoTarget(proto, host, int(port or (26053 if proto == "esphome" else 16700))))
    return out


PROTO = env("PROTO", "esphome")
ECHOS = parse_echos(env("ECHO_HOSTS", ""), PROTO)

STT_URI = env("STT_URI", "tcp://127.0.0.1:10300")
STT_LANG = env("STT_LANG", "it")
TTS_URI = env("TTS_URI", "tcp://127.0.0.1:10200")
TTS_VOICE = env("TTS_VOICE", "it_IT-paola-medium")

LLM_URL = env("LLM_URL", "http://127.0.0.1:8080/v1/chat/completions")
LLM_MODEL = env("LLM_MODEL", "local")
LLM_API_KEY = env("LLM_API_KEY", "")
LLM_STREAM = flag("LLM_STREAM", True)
LLM_TIMEOUT = env("LLM_TIMEOUT", 120.0, float)
LLM_EXTRA = json.loads(env("LLM_EXTRA_JSON", "{}"))

VAD_MODE = env("VAD_MODE", 2, int)
SILENCE_MS = env("SILENCE_MS", 800, int)
NO_SPEECH_S = env("NO_SPEECH_S", 6.0, float)
MAX_REQUEST_S = env("MAX_REQUEST_S", 15.0, float)
KEEPALIVE_S = env("KEEPALIVE_S", 20.0, float)
ERROR_PHRASE = env("ERROR_PHRASE", "Scusa, non riesco a raggiungere l'agente.")

WHISPER_DETECT = flag("WHISPER_DETECT", True)
WHISPER_RATIO = env("WHISPER_RATIO", 0.2, float)
WHISPER_GAIN_DB = env("WHISPER_GAIN_DB", -12.0, float)

# ESPHome
STATE_DIR = env("STATE_DIR", "/var/lib/echo-bridge/state")
HTTP_HOST = env("HTTP_HOST", "0.0.0.0")
HTTP_PORT = env("HTTP_PORT", 8124, int)
PUBLIC_HOST = env("PUBLIC_HOST", "")                 # vuoto: l'indirizzo con cui il container raggiunge l'Echo
API_TOKEN = env("API_TOKEN", "")                     # se impostato, richiesto da /api e /mcp (Authorization: Bearer)
FOLLOWUP = flag("FOLLOWUP", True)                    # risposta che finisce con "?": l'Echo riascolta senza "Alexa"
FOLLOWUP_CONTEXT = flag("FOLLOWUP_CONTEXT", True)    # nella domanda di seguito manda anche la domanda/risposta prima
TIMEZONE = env("TIMEZONE", "")                       # es. Europe/Rome; vuoto: quello del container
HOME_LATITUDE = env("HOME_LATITUDE", "")
HOME_LONGITUDE = env("HOME_LONGITUDE", "")
EVENT_WEBHOOK = env("EVENT_WEBHOOK", "")             # URL a cui inoltrare tasti, eventi e azioni dell'Echo (POST JSON)
ANNOUNCE_TIMEOUT = env("ANNOUNCE_TIMEOUT", 120.0, float)

LOG_LEVEL = env("LOG_LEVEL", "INFO")
