# Richieste accettate dal bridge

Tutto quello che il bridge accetta in ingresso: l'API REST, per i tuoi tool MCP, le automazioni e gli script, e gli
strumenti MCP, per l'agente. Le due strade fanno le stesse cose: ogni strumento MCP corrisponde a una richiesta REST.
Per l'installazione e il funzionamento generale vedi il [README](README.md).

- [Regole comuni](#regole-comuni)
- [Stato: `GET /healthz`, `GET /api/echos`](#stato)
- [Annunci: `POST /api/announce`](#annunci)
- [Timer: `/api/timers`](#timer)
- [Sveglie: `/api/alarms`](#sveglie)
- [Promemoria: `/api/reminders`](#promemoria)
- [Notifiche (anello giallo): `/api/notify`](#notifiche)
- [Musica: `/api/music`](#musica)
- [Modalità notte: `/api/night`](#modalità-notte)
- [Strumenti MCP: `POST /mcp`](#strumenti-mcp)
- [Audio per l'Echo: `GET /audio/…`](#audio-per-lecho)
- [Eventi che il bridge manda al webhook](#eventi-che-il-bridge-manda-al-webhook)

Le richieste valgono per gli Echo in modalità ESPHome, quella predefinita. In modalità Wyoming il bridge gestisce solo
la voce.

---

## Regole comuni

| | |
|---|---|
| **Indirizzo** | `http://<ip-echo-bridge>:8124` (`HTTP_PORT` in `echo-bridge.env`) |
| **Formato** | JSON in ingresso (`Content-Type: application/json`) e in uscita |
| **Autenticazione** | solo se nel bridge è impostato `API_TOKEN`: `Authorization: Bearer <token>` su `/api/…` e `/mcp`, altrimenti HTTP 401 |
| **Errori** | HTTP 400 con `{"error": "…"}`: campo mancante, valore non valido, Echo sconosciuto o scollegato |
| **GET** | le richieste che accettano `GET` leggono gli stessi campi dalla query string, per esempio `GET /api/timers?echo=Echo%20Cucina` |

**Il campo `echo`**, presente in tutte le richieste, sceglie l'Echo:

- nome dell'Echo (quello di `GET /api/echos`), nome del nodo o indirizzo IP;
- **vuoto:** l'Echo che ha ricevuto l'ultima richiesta vocale. Di solito è giusto, perché un tool viene avviato proprio
  da una richiesta fatta a quell'Echo. Se il tool lavora a lungo e in casa ci sono più Echo, conviene che l'agente gli
  passi il nome (lo trova con `list_echos`) e che il tool lo ricordi;
- **`"all"` (o `"tutti"`):** tutti gli Echo collegati.

**Forma della risposta:** con un solo Echo `{"echo": "<nome>", …}`; con `"all"`
`{"all": true, "results": [{"echo": "<nome>", …}, …]}`, un elemento per Echo.

**Orari:** sono nel fuso `TIMEZONE` di `echo-bridge.env` (o in quello del container). Timer, sveglie, promemoria e
notifiche vengono salvati su disco (`STATE_DIR/planner.json`) e sopravvivono a un riavvio del bridge. Se qualcosa
scade mentre l'Echo o il bridge sono spenti, viene consegnato appena tornano, entro 30 minuti; dopo viene saltato,
con una riga nel log.

Negli esempi in Python:

```python
import asyncio, httpx

BRIDGE = "http://192.168.1.30:8124"                      # container echo-bridge
TOKEN = ""                                               # API_TOKEN del bridge, se impostato
AUTH = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}

async def bridge(path: str, body: dict, timeout: float = 15) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as http:
        r = await http.post(f"{BRIDGE}{path}", json=body, headers=AUTH)
        r.raise_for_status()
        return r.json()
```

---

## Stato

**Descrizione.** Gli Echo configurati, se sono collegati e cosa stanno facendo.

| Endpoint | Risposta |
|---|---|
| `GET /healthz` | `{"ok": true, "echos": [{"name", "connected"}]}`; senza autenticazione, per i controlli di salute |
| `GET /api/echos` | una lista con un elemento per Echo: `name`, `node`, `protocol`, `address`, `connected`, `volume`, `media_state`, `music`, `timers`, `alarms`, `reminders`, `notifications` (quante in attesa), `night` |
| `GET /api/echos?entities=1` | come sopra, più `entities` (tutte le impostazioni e i sensori dell'Echo, con il valore attuale) e `actions` |

---

## Annunci

**Descrizione.** L'Echo dice una frase. Serve per avvisare quando un tool ha finito, per esempio "Il backup è finito".

**Endpoint.** `POST /api/announce`

| Campo | | Significato |
|---|---|---|
| `text` | obbligatorio | la frase da dire |
| `echo` | | quale Echo, o `"all"` |
| `listen` | `false` | `true`: dopo l'annuncio l'Echo ascolta la risposta e la manda all'agente come una normale richiesta, senza "Alexa" |

**Risposta.** `{"echo": "…", "played": true}`. `played: false` se l'Echo non l'ha riprodotto, per esempio con "non
disturbare" attivo.

```python
await bridge("/api/announce", {"text": "Il bucato è pronto, vuoi che te lo ricordi tra mezz'ora?", "listen": True}, timeout=150)
```

**Note sull'interazione**

- La chiamata ritorna quando l'Echo ha finito di parlare, al massimo dopo `ANNOUNCE_TIMEOUT` (120 s): usa un timeout
  più lungo.
- La musica si abbassa sotto l'annuncio (Sendspin) oppure va in pausa e poi riprende (media player).
- "Non disturbare" scarta gli annunci, come su Alexa. Per qualcosa da dire più tardi usa un
  [promemoria](#promemoria), che aspetta la fine di "non disturbare"; per qualcosa che può aspettare, una
  [notifica](#notifiche).
- Un tool che lavora a lungo risponde subito all'agente ("ok, ti avviso io") e chiama l'annuncio alla fine,
  in background:

```python
_lavori = set()                                          # tiene vivi i task in background

@mcp.tool()                                              # il tuo server MCP
async def scarica_backup(echo: str = "") -> str:
    async def lavoro():
        ...                                              # il lavoro vero e proprio
        await bridge("/api/announce", {"text": "Il backup è finito.", "echo": echo}, timeout=150)
    t = asyncio.create_task(lavoro()); _lavori.add(t); t.add_done_callback(_lavori.discard)
    return "Backup avviato: ti avviso sull'Echo quando ha finito."
```

---

## Timer

**Descrizione.** Conto alla rovescia sull'Echo, come i timer di Alexa: l'anello mostra il tempo che resta e alla fine
l'Echo suona finché non dici "Alexa, stop" o premi il tasto. Se un tuo tool gestisce dei timer, conviene registrarli
anche qui con la stessa durata, così si sente la suoneria nativa.

**Endpoint.** `POST /api/timers` (`GET /api/timers` = elenco)

| `action` | Altri campi | Effetto |
|---|---|---|
| `start` (default) | `seconds` (obbligatorio), `name` | avvia un timer |
| `cancel` | `name` (nome o id) oppure `all: true` | annulla |
| `pause` / `resume` | `name` | mette in pausa / riprende |
| `add` | `name`, `seconds` (negativo per togliere) | più o meno tempo |
| `list` | | elenco |

**Risposta.** `{"echo": "…", "timers": [{"id", "name", "total_seconds", "seconds_left", "active"}]}`: i timer
avviati, cambiati o annullati, o l'elenco.

```python
await bridge("/api/timers", {"action": "start", "seconds": 600, "name": "pasta"})
```

**Note.** Senza `name`, `cancel`, `pause` e `resume` agiscono sull'unico timer in corso. Con `"echo": "all"` il
timer parte su ogni Echo.

---

## Sveglie

**Descrizione.** Sveglie a un orario, anche ricorrenti. Suonano con la suoneria dei timer, finché non dici "Alexa,
stop" o premi il tasto. Suonano anche con "non disturbare" attivo, come su Alexa.

**Endpoint.** `POST /api/alarms` (`GET /api/alarms` = elenco)

| `action` | Altri campi | Effetto |
|---|---|---|
| `set` (default) | `time` (obbligatorio, `"07:00"`), `days`, `date`, `name` | imposta una sveglia |
| `cancel` | `name` (nome, orario `"07:00"` o id) oppure `all: true` | cancella |
| `list` | | elenco |

- `days`: per ripeterla. `"feriali"`, `"weekend"`, `"tutti"`, oppure giorni (`"lun,mer,ven"` o
  `["lun", "mer"]`; vanno bene anche i nomi inglesi e i numeri, 0 = lunedì).
- `date`: `"AAAA-MM-GG"` per un giorno preciso.
- Senza `days` né `date`: la prossima volta che è quell'ora, cioè oggi o domani.
- `name`: di default "Sveglia".

**Risposta.** `{"echo": "…", "alarms": [{"id", "name", "time", "days", "date", "next"}]}`. `next` è la prossima
volta che suona (`"2026-11-26 07:00"`).

```python
await bridge("/api/alarms", {"time": "07:00", "days": "feriali", "name": "Lavoro"})
```

**Note.** Una sveglia senza ripetizione viene tolta dopo aver suonato. Un orario non valido o una data già passata
danno errore 400.

---

## Promemoria

**Descrizione.** All'ora stabilita l'Echo dice "Promemoria: <testo>". Possono ripetersi.

**Endpoint.** `POST /api/reminders` (`GET /api/reminders` = elenco)

| `action` | Altri campi | Effetto |
|---|---|---|
| `set` (default) | `text` (obbligatorio) e uno tra `time`, `at`, `in_minutes`; poi `days`, `date`, `listen` | imposta |
| `cancel` | `id`, oppure `text` (una parola del testo), oppure `all: true` | cancella |
| `list` | | elenco |

- `time`: `"18:00"`, con `days` (come le sveglie) per ripeterlo, o `date`.
- `at`: data e ora ISO, `"2026-11-26T18:00"`.
- `in_minutes`: tra quanti minuti.
- `listen`: `true` per far ascoltare all'Echo la risposta dopo il promemoria.

**Risposta.** `{"echo": "…", "reminders": [{"id", "text", "next", "time", "days", "listen"}]}`

```python
await bridge("/api/reminders", {"text": "chiamare Marco", "time": "18:00"})
await bridge("/api/reminders", {"text": "buttare la plastica", "time": "21:00", "days": "mar"})
```

**Note sull'interazione**

- **"Non disturbare":** con "non disturbare" attivo, anche quello della [modalità notte](#modalità-notte), il
  promemoria aspetta che finisca, come su Alexa.
- **Echo impegnato:** se l'Echo non riesce a riprodurlo, per esempio perché sta parlando, il bridge riprova ogni
  minuto.
- Un promemoria senza ripetizione viene tolto dopo essere stato detto.

---

## Notifiche

**Descrizione.** Come le notifiche di Alexa: un tool lascia un messaggio senza interrompere, l'anello dell'Echo pulsa
giallo, e quando chiedi "Alexa, che notifiche ho?" l'agente le legge con lo strumento MCP `read_notifications`, che
spegne anche l'anello. Sono adatte a cose non urgenti: un pacco consegnato, un download finito durante la notte.

**Endpoint.** `POST /api/notify` (`GET /api/notify` = elenco)

| `action` | Altri campi | Effetto |
|---|---|---|
| `add` (default) | `text` (obbligatorio), `source` (chi la manda, per esempio `"backup"`) | lascia una notifica, anello giallo |
| `list` | | elenco, senza segnarle come lette |
| `read` / `clear` | `id` (facoltativo: una sola) | restituisce le notifiche e le segna come lette; spento l'anello se non ne restano |

**Risposta.** `{"echo": "…", "notifications": [{"id", "text", "source", "at"}]}`, più `"cleared": true` con
`read` e `clear`.

```python
await bridge("/api/notify", {"text": "Il pacco è stato consegnato", "source": "corriere", "echo": "all"})
```

**Note sull'interazione**

- **L'anello giallo** usa la luce "Light ring" di hassmic, che esiste se l'Echo ha installato le sue animazioni
  dell'anello (succede al primo avvio). Le luci proprie dell'Echo, come ascolto, risposta e volume, vengono prima, e
  il giallo torna dopo.
- Senza la luce dell'anello le notifiche funzionano lo stesso: restano in attesa finché l'agente non le legge.
  Questo caso è l'unico provato sul simulatore, dove la luce manca.
- Le notifiche restano salvate anche se il bridge si riavvia, e l'anello si riaccende quando l'Echo si ricollega.
- Perché l'agente risponda a "che notifiche ho?", deve avere a disposizione lo strumento MCP `read_notifications`
  del bridge.

---

## Musica

**Descrizione.** Musica sull'Echo da un URL, anche da YouTube. Il bridge decodifica la sorgente con ffmpeg e la manda
al lettore Sendspin dell'Echo: come su Alexa, quando dici "Alexa" o arriva un annuncio la musica si abbassa senza
fermarsi. Senza Sendspin (`SENDSPIN=0`) passa dal media player: il bridge la mette in pausa quando parli e poi la
riprende.

**Endpoint.** `POST /api/music` (`GET /api/music` = stato)

| `action` | Altri campi | Effetto |
|---|---|---|
| `play` (default se c'è `url`) | `url` (obbligatorio), `headers`, `title`, `seek`, `live` | riproduce; sostituisce il brano in corso |
| `pause` / `resume` / `stop` | | pausa, ripresa dal punto in cui era, stop |
| `sleep` | `minutes` (0 annulla), `fade` (secondi, default 30) | si ferma tra N minuti, abbassandosi piano negli ultimi `fade` secondi, poi il volume torna com'era |
| `status` | | stato |

- `url`: l'URL diretto dell'audio (con yt-dlp `info["url"]`, non il link della pagina).
- `headers`: le intestazioni HTTP che la sorgente vuole (con yt-dlp `info["http_headers"]`).
- `seek`: da quale secondo partire.
- `live: true`: radio e dirette, che non si possono riavvolgere.

**Risposta.** `{"echo": "…", "state", "title", "url", "position", "live", "backend", "sleep"}`

- `state`: `playing`, `paused` o `idle`. Nel ripiego dal media player anche `interrupted`, cioè in pausa per voce o
  annuncio, e riprende da sola.
- `backend`: `sendspin` o `url`.
- `sleep`: `null`, oppure `{"stops_at", "seconds_left", "fade_seconds"}`.

Esempio di un tool che cerca su YouTube e suona sull'Echo. Il tool continua a fare quello che fa oggi; cambia solo
l'ultimo passaggio:

```python
import yt_dlp

def audio_youtube(ricerca: str) -> dict:                 # quello che il tool fa già oggi
    opts = {"format": "bestaudio/best", "quiet": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch1:{ricerca}", download=False)["entries"][0]
    return {"url": info["url"], "headers": info.get("http_headers", {}), "title": info.get("title", ricerca)}

@mcp.tool()
async def riproduci(ricerca: str, echo: str = "") -> str:
    audio = await asyncio.to_thread(audio_youtube, ricerca)          # yt-dlp è bloccante
    await bridge("/api/music", {"action": "play", **audio, "echo": echo})
    return f"In riproduzione sull'Echo: {audio['title']}"

@mcp.tool()
async def musica(comando: str, echo: str = "") -> str:              # pause, resume, stop
    return (await bridge("/api/music", {"action": comando, "echo": echo}))["state"]
```

**Note sull'interazione**

| Succede | Con Sendspin (default) | Nel ripiego dal media player |
|---|---|---|
| Dici "Alexa" | la musica si abbassa, l'Echo risponde sopra, poi torna al volume di prima | pausa, risposta, ripresa dal punto in cui era |
| Arriva un annuncio | la musica si abbassa sotto l'annuncio | pausa, annuncio, ripresa |
| L'agente avvia la musica mentre sta ancora rispondendo | parte subito, abbassata finché la risposta non è finita | parte a risposta finita |
| "Alexa, stop" | pausa, non riprende da sola | pausa, non riprende da sola |
| Tasto azione | pausa; premuto di nuovo, riprende | nuova richiesta, come "Alexa" |
| Il brano finisce | `idle`, e al webhook l'evento `finished` | uguale |
| La sorgente non si apre (URL scaduto, 403) | `idle`, e al webhook l'evento `error` | uguale |

- **Coda e playlist:** le gestisce il tool. Quando arriva l'evento `finished` al webhook (`EVENT_WEBHOOK`), oppure
  controllando `GET /api/music` ogni tanto, il tool manda il `play` del brano successivo.
- **URL di YouTube:** scadono dopo qualche ora e valgono solo per l'IP pubblico che li ha ricavati. Il tool e il
  bridge dietro lo stesso router vanno bene. Se il tool esce da un'altra connessione, per esempio una VPN, deve
  scaricare il file e servirlo sulla rete locale.
- **Music Assistant:** il lettore Sendspin dell'Echo accetta un solo server alla volta, il bridge o Music Assistant.
- **Volume:** strumento MCP `set_volume`, oppure i tasti dell'Echo. Lo `sleep` riporta il volume com'era.

---

## Modalità notte

**Descrizione.** Come la modalità notte di Alexa: in una fascia oraria l'Echo è in "non disturbare" (niente annunci,
i promemoria aspettano; timer e sveglie suonano comunque) e il volume è più basso. La fascia si imposta in
`echo-bridge.env` con `NIGHT_START`, `NIGHT_END`, `NIGHT_DND` e `NIGHT_VOLUME`.

**Endpoint.** `POST /api/night` (`GET /api/night` = stato)

| `action` | Effetto |
|---|---|
| `status` (default) | stato |
| `on` | modalità notte adesso, fino al prossimo cambio di fascia |
| `off` | spenta adesso, fino al prossimo cambio di fascia |
| `auto` | torna a seguire la fascia oraria |

**Risposta.** `{"echo": "…", "active", "mode", "window", "dnd", "volume"}`

```python
await bridge("/api/night", {"action": "off", "echo": "Echo Cucina"})     # stasera niente modalità notte in cucina
```

**Note sull'interazione**

- **Fine della modalità:** "non disturbare" si spegne e il volume torna a quello di prima.
- **Override:** `on` e `off` valgono fino al prossimo cambio di fascia, poi torna automatica. Un riavvio del bridge
  annulla l'override.

---

## Strumenti MCP

**Descrizione.** Il bridge è anche un server MCP: l'agente comanda gli Echo con questi strumenti. Si aggiunge ai
server MCP dell'agente con l'indirizzo qui sotto.

**Endpoint.** `POST /mcp` (MCP streamable HTTP), cioè `http://<ip-echo-bridge>:8124/mcp`

Tutti accettano `echo`, con le stesse regole delle richieste REST, compreso `"all"`.

| Strumento | Parametri | Richiesta REST equivalente |
|---|---|---|
| `list_echos` | | `GET /api/echos` |
| `echo_status` | `echo` | `GET /api/echos?entities=1` |
| `announce` | `text`, `listen` | [`POST /api/announce`](#annunci) |
| `set_timer`, `list_timers`, `cancel_timer`, `pause_timer`, `change_timer` | `seconds`, `name`, `all`, `pause` | [`/api/timers`](#timer) |
| `set_alarm`, `list_alarms`, `cancel_alarm` | `time`, `days`, `date`, `name`, `all` | [`/api/alarms`](#sveglie) |
| `set_reminder`, `list_reminders`, `cancel_reminder` | `text`, `time`, `at`, `in_minutes`, `days`, `date`, `listen`, `text_or_id`, `all` | [`/api/reminders`](#promemoria) |
| `notify`, `read_notifications` | `text`, `source`, `clear` | [`/api/notify`](#notifiche) |
| `media` | `url`, `title`, `headers`, `seek`, `live`, oppure `command` (`pause`, `resume`, `stop`, `status`, `mute`, `unmute`, `volume_up`, `volume_down`) | [`/api/music`](#musica) |
| `music_sleep` | `minutes`, `fade` | [`/api/music`](#musica) con `sleep` |
| `set_volume` | `level` (0-100) | |
| `night_mode` | `mode` (`on`, `off`, `auto`, `status`) | [`/api/night`](#modalità-notte) |
| `set_entity` | `entity`, `value`, `brightness`, `rgb`, `effect` | impostazioni dell'Echo: non disturbare, suoni, luce dell'anello, equalizzatore, wake word, aggiornamento (`value: install`)… |
| `run_action` | `action`, `data` | azioni dell'Echo, per esempio `drop_in` con `{"target": "Echo Cucina"}` |

**Note sull'interazione**

- **Echo di default:** senza `echo` gli strumenti agiscono sull'Echo da cui l'utente sta parlando, quindi "metti un
  timer di 10 minuti" va sull'Echo giusto senza che l'agente debba indicarlo.
- **Autenticazione:** con `API_TOKEN` impostato, il client MCP dell'agente deve mandare
  `Authorization: Bearer <token>`.

---

## Audio per l'Echo

**Descrizione.** Uso interno: da qui l'Echo scarica le risposte vocali, gli annunci e, nel ripiego dal media player,
la musica. Gli URL li genera il bridge, quindi un tool non deve chiamarli.

**Endpoint.** `GET /audio/<id>.wav` (voce) e `GET /audio/<id>.mp3` (musica)

**Note.**

- **Raggiungibilità:** l'Echo deve raggiungere il container su questa porta. Con più schede di rete imposta
  `PUBLIC_HOST`.
- **Accesso:** questo percorso non chiede il token, perché l'Echo non saprebbe mandarlo. Gli id sono casuali e
  scadono dopo 10 minuti.

---

## Eventi che il bridge manda al webhook

Non sono richieste in ingresso, ma completano l'interazione con i tool. Se `EVENT_WEBHOOK` è impostato, il bridge
manda un `POST` JSON a quell'indirizzo per ogni evento. Un tool può riceverli per reagire, per esempio per far partire
il brano successivo.

| Evento | Corpo |
|---|---|
| tasto premuto o tenuto 1 s | `{"echo", "type": "event", "entity": "button", "event": "action"}`; gli eventi sono `action`, `action_hold`, `volume_up`, `volume_up_hold`, `volume_down`, `volume_down_hold` |
| wake word | `{"echo", "type": "event", "entity": "wake_word", "event": "detected"}`; gli eventi sono `detected`, `near_miss`, `answered_elsewhere` (ha risposto un altro Echo), `no_speech` |
| esito di un Drop In | `{"echo", "type": "event", "entity": "drop_in_result", "event": "…"}` |
| musica: brano finito | `{"echo", "type": "music", "event": "finished", "title"}` |
| musica: sorgente non riproducibile | `{"echo", "type": "music", "event": "error", "title", "error"}` |
| musica: pausa dall'Echo ("stop", tasto) | `{"echo", "type": "music", "event": "paused", "title"}` |
| azioni che l'Echo chiede e il bridge non gestisce | `{"echo", "type": "action", "service", "data"}` |
