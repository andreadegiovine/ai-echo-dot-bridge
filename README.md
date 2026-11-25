# ai-echo-dot-bridge

[![CI](https://github.com/andreadegiovine/ai-echo-dot-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/andreadegiovine/ai-echo-dot-bridge/actions/workflows/ci.yml)

**Echo Dot 3 → altoparlante del tuo agente locale, senza cloud e senza Home Assistant.**

L'Echo viene sbloccato con [echo-dot-assist (hassmic)](https://github.com/Gamer92000/echo-dot-assist). Questo
repository contiene il bridge che prende il posto di Home Assistant: parla con l'Echo con lo stesso protocollo
(ESPHome) e lo collega a faster-whisper, Piper e al tuo agente (llama.cpp + server MCP), tutto in un container LXC
su Proxmox. Così restano tutte le funzioni native di hassmic (timer, annunci, domande di seguito, sussurro, più Echo
che non rispondono tutti insieme), e il bridge le offre all'agente come strumenti MCP.

| File | Cosa contiene |
|---|---|
| [`echobridge/`](echobridge) | il bridge: [ESPHome](echobridge/esphome_echo.py) (default) e [Wyoming](echobridge/wyoming_echo.py), [voce e agente](echobridge/speech.py), [timer](echobridge/timers.py), [server HTTP e MCP](echobridge/server.py) |
| [`install.sh`](install.sh) | installazione nel container: librerie, utente e servizi systemd |
| [`echo-bridge.env`](echo-bridge.env) | configurazione di esempio (finisce in `/etc/echo-bridge/`) |
| [`selftest.py`](selftest.py) | autotest di STT, TTS e agente, senza Echo |
| [`tests/`](tests) | test end-to-end contro il simulatore di hassmic; li esegue la [CI](.github/workflows/ci.yml) |

## Come funziona

```
 Echo Dot 3 sbloccato (hassmic, ESPHome)                  LXC "echo-bridge" su Proxmox
 ┌──────────────────────────────────────────┐  ESPHome   ┌────────────────────────────────────────┐
 │ wake word "Alexa" (motore Amazon, locale)│  cifrato   │ echobridge (fa la parte di Home Ass.)  │
 │ microfoni + cancellazione eco Amazon     │  :26053    │  ├─ VAD (fine frase)                   │
 │ tasti, mute hardware, anello LED         │◀──────────▶│  ├─ faster-whisper :10300 (STT, it)    │
 │ timer, annunci, sussurro (modello Amazon)│            │  ├─ agente (API OpenAI) ───────────────┼──▶ llama.cpp
 │ arbitraggio tra più Echo, Drop In        │  HTTP      │  ├─ Piper :10200 (TTS, it)             │
 │ firewall: solo rete locale, mai Amazon   │──────────▶ │  └─ :8124 audio delle risposte, MCP ◀──┼─── agente (strumenti)
 └──────────────────────────────────────────┘  :8124     └────────────────────────────────────────┘
```

- **Sull'Echo** restano solo le parti che Amazon fa bene: wake word "Alexa", cancellazione dell'eco, tasti e LED.
  Il client Alexa è spento e un firewall impedisce qualsiasi contatto con Amazon.
- **Il container** fa quello che farebbe Home Assistant: capisce quando hai finito di parlare, trascrive, chiede
  all'agente e gli fa leggere la risposta. Le frasi vengono lette man mano che l'agente le scrive. In più tiene i timer,
  fa gli annunci, imposta la cifratura e l'ora e passa le chiavi tra più Echo.
- **L'agente** può comandare gli Echo con gli strumenti MCP del bridge: timer, annunci, volume, impostazioni, Drop In.
- **Nessun passthrough hardware:** l'Echo è in Wi-Fi e il container ci parla via rete. Basta un container non
  privilegiato.

Il bridge è provato end-to-end contro il simulatore di hassmic (`build/hassmic-host` del progetto upstream),
con STT, TTS e agente finti. La [CI](https://github.com/andreadegiovine/ai-echo-dot-bridge/actions/workflows/ci.yml)
ripete i test a ogni push. In modalità ESPHome coprono: cifratura impostata dal bridge e riusata dopo un riavvio, ora
data all'Echo, wake word, fine frase, risposta in streaming, domanda di seguito senza "Alexa", sussurro dal sensore
nativo dell'Echo, timer (avvio, pausa, annullo, suoneria, anche via REST), annunci, volume, "non disturbare", tasti
come eventi e webhook, due Echo che entrano nella stessa rete di arbitraggio, agente lento oltre i 30 s, musica da una
sorgente Opus come quelle di YouTube (conversione, pausa e ripresa per voce e annunci, "Alexa, stop", coda durante una
risposta, fine del brano), attraverso gli strumenti MCP e l'API REST. In modalità Wyoming: voce, sussurro stimato, agente lento. **Non è stato provato su un Echo vero, né
con Whisper e Piper veri**: la prima prova reale sarà la tua.

---

## 0. Prima di comprare il saldatore: leggi qui

1. **Modello.** Sotto l'Echo, sull'etichetta, deve esserci **D9N29T** (Echo Dot 3, 2018). Il **C78MP8** (2019–2020) e
   il **36EBT3** (quello con l'orologio) **non sono supportati**.
2. **Rischio di brick reale.** Su alcuni Echo aggiornati di recente lo sblocco termina senza errori, ma il dispositivo
   resta bloccato con l'anello verde e per ora non si può recuperare (issue #112). Controlla lo stato della issue su
   <https://github.com/Gamer92000/echo-dot-assist/issues/112>. Finché è aperta, usa un Echo che puoi permetterti di perdere.
3. **Non collegare più l'Echo ad Amazon**, nemmeno "per provare". Un aggiornamento automatico può chiudere la falla
   usata per lo sblocco. Se è ancora configurato nella tua app Alexa, **non** rimuoverlo mentre è online.
4. Il progetto si definisce *"vibecoded, unaudited, your risk"* (scritto con un'AI, non verificato, a tuo rischio).

## Cosa ti serve

| Cosa | Note |
|---|---|
| Echo Dot 3 D9N29T + il suo alimentatore | |
| Cavo USB **con spina USB-A** da sacrificare | Con USB-C serve una resistenza da 5,1 kΩ sul filo CC |
| Saldatore a punta fine, stagno, nastro isolante | In alternativa i supporti a pogo-pin stampabili in 3D ([link nella guida del modello](https://github.com/Gamer92000/echo-dot-assist/blob/main/devices/donut/README.md#usb-access)) |
| Attrezzi per aprire la scocca (plettri, Torx) | Guide di apertura nella discussione XDA |
| **PC con Linux installato direttamente** | Non una VM e non WSL. Va bene anche Ubuntu da chiavetta live, ma tieni il repository su un disco vero |
| Proxmox con spazio per un container da 4 core / 4 GB RAM / 16 GB disco | |
| llama.cpp (`llama-server`) raggiungibile dalla rete | |

---

## Parte A — Il container su Proxmox

### A1. Crea il container con il community script

Nella **shell del nodo Proxmox** (Datacenter → nodo → Shell):

```bash
var_hostname=echo-bridge var_cpu=4 var_ram=4096 var_disk=16 var_version=13 \
bash -c "$(curl -fsSL https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/ct/debian.sh)"
```

- Lo script crea un container Debian 13 **non privilegiato**. Le variabili `var_*` cambiano i valori predefiniti
  (1 core e 512 MB sono troppo pochi per Whisper).
- Se il menu chiede "Default" o "Advanced", scegli **Advanced** e controlla CPU 4, RAM 4096, disco 16 e hostname
  `echo-bridge`. Dagli un **IP statico** o una riserva DHCP.
- Risorse indicative: `small-int8` di Whisper usa circa 1 GB di RAM e 1–2 s per frase su 4 core moderni. Piper è
  leggerissimo.

### A2. Scarica il bridge nel container

Entra nel container (`pct enter <CTID>`, dove `<CTID>` è il numero del container, per esempio 120) e clona il
repository:

```bash
apt-get update && apt-get install -y git
git clone https://github.com/andreadegiovine/ai-echo-dot-bridge.git /root/ai-echo-dot-bridge
cd /root/ai-echo-dot-bridge
```

**Aggiornare in seguito:** `cd /root/ai-echo-dot-bridge && git pull && bash install.sh`. La configurazione in
`/etc/echo-bridge/echo-bridge.env` non viene sovrascritta. Poi `systemctl restart echo-bridge`.

### A3. Installa STT, TTS e bridge

```bash
bash install.sh
# Opzioni: WHISPER_MODEL=medium-int8 bash install.sh   (più preciso in italiano, circa 3 volte più lento)
#          PIPER_VOICE=it_IT-riccardo-x_low bash install.sh (voce maschile)
```

Lo script:

- installa Python, crea l'utente `echobridge` e il venv in `/opt/echo-bridge` con le librerie sotto;
- crea tre servizi systemd: `wyoming-whisper`, `wyoming-piper` ed `echo-bridge`;
- al primo avvio scarica il modello Whisper e la voce Piper da Hugging Face, quindi il container deve uscire su internet.

| Libreria | A cosa serve |
|---|---|
| `wyoming` 1.11.0 | protocollo per parlare con Whisper e Piper (e con l'Echo in modalità Wyoming) |
| `wyoming-faster-whisper` 3.9.1 | server STT (faster-whisper / CTranslate2) |
| `wyoming-piper` 2.5.4 | server TTS (Piper) |
| `webrtcvad-wheels` 2.0.14 | rileva quando hai smesso di parlare |
| `httpx` 0.28.1 | chiamate in streaming all'agente |
| `numpy` 2.5.5 | riconoscimento del sussurro e attenuazione dell'audio |
| `ffmpeg` (pacchetto Debian) | converte la musica in MP3 per l'Echo |
| `aioesphomeapi` 46.25.0 | protocollo ESPHome: la libreria di Home Assistant, la stessa versione dei test di hassmic |
| `mcp` 2.3.0, `uvicorn`, `starlette` | server HTTP del bridge: audio per l'Echo e strumenti MCP per l'agente |

### A4. Collega l'agente

Nel container di llama.cpp, `llama-server` deve ascoltare sulla rete (`--host 0.0.0.0 --port 8080`). Dal container
echo-bridge:

```bash
curl http://<ip-llama>:8080/v1/models
```

Poi modifica la configurazione:

```bash
nano /etc/echo-bridge/echo-bridge.env
```

- `LLM_URL`: punta all'endpoint OpenAI-compatibile.
  - **llama.cpp diretto**: `http://<ip-llama>:8080/v1/chat/completions`. Il modello risponde, ma **senza strumenti**.
  - **Il tuo agente con gli strumenti** (Home Assistant e il resto): metti qui il suo endpoint se espone
    `/v1/chat/completions` in streaming SSE. Se ha un'API diversa, basta riscrivere la funzione `ask_agent()` in
    [`echobridge/speech.py`](echobridge/speech.py): deve restituire il testo a pezzi. Tutto il resto non cambia.
- `LLM_EXTRA_JSON`: per i modelli "thinking" (Qwen3 e simili) conviene disattivare il ragionamento, altrimenti
  l'Echo resta in silenzio a lungo. Il bridge comunque scarta `<think>…</think>` e `reasoning_content`.
- Prompt di sistema, contesto e strumenti li gestisce llama.cpp: il bridge manda solo la domanda trascritta, come
  unico messaggio `user`, e nel campo `user` della richiesta il nome dell'Echo che ha chiesto. Dalla risposta toglie
  comunque `<think>`, `*`, `#`, URL ed emoji, perché viene letta ad alta voce.
- **Unica eccezione, le domande di seguito:** se la risposta finisce con "?" l'Echo riascolta senza "Alexa", e nella
  richiesta successiva il bridge mette anche la domanda e la risposta di prima, altrimenti un "sì" non avrebbe senso.
  Per non mandarle: `FOLLOWUP_CONTEXT=0`. Per non riascoltare affatto: `FOLLOWUP=0`.

### A5. Autotest del container (ancora senza Echo)

```bash
set -a; . /etc/echo-bridge/echo-bridge.env; set +a
/opt/echo-bridge/venv/bin/python /opt/echo-bridge/selftest.py
```

Atteso, più o meno:

```
TTS  ok: 2.6 s di audio a 22050 Hz in 0.4 s
STT  ok: 'Accendi la luce del soggiorno, per favore.' in 1.3 s
LLM  ok: 'Tutto funziona.' (prima frase dopo 0.8 s, totale 0.9 s)
```

Se tutte e tre le righe vanno, la parte server è pronta. **Non avviare ancora `echo-bridge`**: manca l'Echo.

### A6. Dai all'agente gli strumenti degli Echo (MCP)

Il bridge è anche un server MCP, all'indirizzo `http://<ip-echo-bridge>:8124/mcp` (streamable HTTP). Aggiungilo ai
server MCP del tuo agente accanto a quello che già usi. Se imposti `API_TOKEN` in `echo-bridge.env`, l'agente deve
mandare `Authorization: Bearer <token>`.

| Strumento | Cosa fa |
|---|---|
| `set_timer`, `list_timers`, `cancel_timer`, `pause_timer`, `change_timer` | timer sull'Echo: l'anello mostra il tempo, alla fine suona finché non lo fermi ("Alexa, stop" o tasto) |
| `announce` | fa dire un testo a un Echo; con `listen=true` poi ascolta la risposta e la passa all'agente |
| `set_volume`, `media` | volume; musica da un URL (anche YouTube, vedi [D2](#d2-musica-dal-tool-sullecho)): play, pause, resume, stop, status |
| `echo_status`, `set_entity` | tutte le impostazioni e i sensori dell'Echo: non disturbare, suoni, luce dell'anello, equalizzatore, wake word, aggiornamenti… |
| `run_action` | azioni dell'Echo, per esempio `drop_in` con `{"target": "Echo Cucina"}` per chiamare un altro Echo |
| `list_echos` | Echo collegati, volume e timer in corso |

Senza il parametro `echo` gli strumenti agiscono sull'Echo che ha ricevuto l'ultima richiesta, cioè quello da cui ti
sta parlando chi ha chiesto "metti un timer di 10 minuti". Con più Echo l'agente può nominarli, per esempio
`echo: "Echo Cucina"`.

C'è anche una piccola API REST, per i tuoi tool MCP, automazioni e script: annunci, timer e musica. Come usarla
dai tool è spiegato nella [parte D](#parte-d--i-tuoi-tool-mcp-e-lecho).

```bash
curl -X POST http://<ip-echo-bridge>:8124/api/announce -H 'Content-Type: application/json' \
     -d '{"text": "La lavatrice ha finito", "echo": "Echo Cucina"}'
curl http://<ip-echo-bridge>:8124/api/echos
```

---

## Parte B — Lo sblocco dell'Echo

Tutta questa parte si fa dal **PC Linux** con il cavo USB saldato, non da Proxmox.

> Proxmox è un Debian installato direttamente sulla macchina, quindi tecnicamente funzionerebbe. Lo sconsiglio: dovresti
> installare strumenti di sviluppo sull'hypervisor, e durante l'exploit l'Echo cambia identità USB più volte, cosa che
> non passa in un container. Un portatile con Ubuntu 24.04, anche da chiavetta live, è la scelta giusta.

### B1. Prepara il PC

```bash
sudo apt update
sudo apt install -y git adb fastboot python3 python3-venv python3-pip make unzip e2fsprogs curl
git clone https://github.com/Gamer92000/echo-dot-assist.git
cd echo-dot-assist
sudo cp scripts/51-echo-unlock.rules /etc/udev/rules.d/ && sudo udevadm control --reload
```

Le regole udev permettono l'accesso USB senza sudo e tengono ModemManager lontano dall'Echo. Su Ubuntu puoi anche
fermarlo del tutto: `sudo systemctl stop ModemManager`.

Scarica **esattamente** questo firmware da FTVDB (link in [`devices/donut/README.md`](https://github.com/Gamer92000/echo-dot-assist/blob/main/devices/donut/README.md#requirements)) e mettilo in `firmware/donut/`:

```
update-kindle-donut_puffin-NS65741_user_8138_0013222529668.bin
sha256: ac22b78cf94c2ebacfa90447b770b803d9f72f35858d5218975d97fea95a0245
```

```bash
mkdir -p firmware/donut && mv ~/Downloads/update-kindle-donut_puffin-NS65741_*.bin firmware/donut/
sha256sum firmware/donut/*.bin    # deve coincidere
```

Usa questa versione anche se l'Echo ne ha una più nuova: con un'altra versione la procedura si ferma.

### B2. Apri l'Echo e salda il cavo

**Echo scollegato dalla corrente.** Apri la scocca seguendo le guide linkate nella discussione XDA, poi:

| Filo del cavo USB | Pad sulla scheda |
|---|---|
| nero (GND) | **TM3** (oppure lo schermo metallico) |
| bianco (D−) | **TM4** |
| verde (D+) | **TM5** |
| rosso (+5 V) | **niente**: taglialo corto e isolalo |

L'Echo si alimenta dal **suo** alimentatore, mai dall'USB. Tienilo staccato da corrente e USB finché la procedura XDA
non dice di collegarlo.

### B3. Strada consigliata: installer guidato

```bash
scripts/setup.sh donut
```

- Su un commit già compilato da GitHub usa quei binari, verificati con la chiave del progetto, quindi niente NDK da 1 GB.
- Chiede di scrivere `yes` perché **cancella l'Echo**.
- Si ferma da solo nei passaggi manuali: download, saldatura, sblocco e password Wi-Fi.

Quello che succede, e dove intervieni tu:

1. **Sblocco a mano dalla discussione XDA**
   (<https://xdaforums.com/t/unlock-root-twrp-unbrick-amazon-echo-dot-3rd-gen-2018-donut.4801400/>). L'autore
   dell'exploit chiede che **non** venga mai lanciato da script, quindi l'installer ti manda lì e aspetta. Segui il
   primo post (exploit `kamakiri-donut`) e **fermati quando TWRP è attivo (anello bianco)**. Torna all'installer e
   premi Invio.
2. **Firmware 6574.1 su entrambi gli slot A/B.** Lo fa l'installer. **Non** usare il comando `bcbtool set_active`
   della discussione XDA: TWRP cambia slot da solo.
3. **Root:** installa `boot-root` da TWRP **a mano**, con il file della discussione XDA. L'installer controlla che
   `adb shell id -u` restituisca `0`.
4. Scarica i binari, esegue `probe.sh` (che verifica il firmware), poi deploy e **lockdown**. Il firewall viene attivato
   **prima** che l'Echo veda la tua rete.
5. Chiede SSID e password, collega l'Echo al Wi-Fi e mostra l'IP. **Crea subito una riserva DHCP sul router.**
6. `install-system.sh` scrive hassmic nella partizione di sistema e riavvia. Dopo circa un minuto l'Echo è un
   satellite (anello spento o arancione di attesa).

Se si interrompe: Ctrl-C, e rilanciandolo riprende da dove era. Il log completo è in `build/donut/setup.log`.

<details>
<summary>Stessa cosa a mano, comando per comando (dal README del modello)</summary>

```bash
# dopo lo sblocco XDA, con TWRP attivo (anello bianco):
F=firmware/donut/update-kindle-donut_puffin-NS65741_user_8138_0013222529668.bin
adb shell twrp wipe cache; adb shell twrp wipe data
adb push $F /sdcard/update.zip
adb shell twrp install /sdcard/update.zip      # "Flashing A/B zip to inactive slot: B" (o A)
adb reboot recovery                            # attendi l'anello bianco
adb shell twrp install /sdcard/update.zip      # deve indicare l'ALTRO slot
adb reboot recovery
# boot-root da TWRP, a mano, come dice XDA, poi:
adb shell id -u                                # 0

export PREBUILT=1
scripts/probe.sh                               # non deve segnalare librerie DIVERSE
scripts/deploy.sh
adb shell sh /data/local/hassmic/lockdown.sh   # firewall: solo indirizzi locali
mkdir -p secrets && printf '%s\n%s\n' 'Mio SSID' 'mia password' > secrets/wifi.conf
scripts/wifi-join.sh                           # stampa l'IP. NON riavviare prima del passo dopo
scripts/install-system.sh                      # installa e riavvia
```
</details>

### B4. Modalità ESPHome: non c'è niente da cambiare

hassmic parla ESPHome di default (porta 26053), ed è proprio quello che usa il bridge. Al primo collegamento il bridge
genera la chiave di cifratura e la imposta sull'Echo, come farebbe Home Assistant. Da lì in poi entra solo lui.

- **Se l'Echo era già in Home Assistant**, ha la chiave di Home Assistant e il bridge non può entrare (nel log:
  `l'Echo ha già una chiave che il bridge non conosce`). Elimina il dispositivo in Home Assistant, che cancella la
  chiave, oppure con il cavo: `adb shell rm /data/local/hassmic/state/api_key` e riavvia l'Echo.
- **Non collegare insieme** Home Assistant e il bridge allo stesso Echo: l'assistente vocale va a uno solo.
- Opzione utile in `ARGS` di `/data/local/hassmic/hassmic.conf` (poi riavvia): `-E`, nessun suono quando senti "Alexa".
- **Non** aggiungere `-w remote`: la wake word la riconosce l'Echo, ed è il motore originale di Amazon.

<details>
<summary>In alternativa: modalità Wyoming (meno funzioni)</summary>

Wyoming è un protocollo più semplice: restano voce, tasti, anello e sussurro stimato dal bridge. Mancano cifratura,
timer, annunci, domande di seguito, sussurro nativo, arbitraggio, Drop In e strumenti MCP. Si attiva con il cavo:

```bash
adb shell 'echo PROTO=wyoming >> /data/local/hassmic/hassmic.conf'
adb reboot
```

e nel bridge `ECHO_HOSTS=wyoming://<ip-echo>` (porta 16700).
</details>

### B5. Prima di staccare i fili

- **Fai il backup di `secrets/update.key`.** Firma gli aggiornamenti di hassmic ed è l'unico modo di avere adb via
  Wi-Fi senza Home Assistant (`scripts/adb-wifi.sh <ip-echo>`, aperto 30 minuti).
- Controlla dal container che la porta sia aperta: `nc -zv <ip-echo> 26053`.
- Solo dopo dissalda, richiudi e riavvia.

---

## Parte C — Accendi tutto

```bash
# nel container echo-bridge
nano /etc/echo-bridge/echo-bridge.env        # ECHO_HOSTS=<ip-echo>
systemctl enable --now echo-bridge
journalctl -fu echo-bridge
```

Al primo avvio il bridge imposta la chiave e si collega. Di' **"Alexa, che ore sono?"**. Nel log dovresti vedere:

```
INFO 192.168.1.50:26053: chiave di cifratura impostata e salvata
INFO 192.168.1.50:26053: collegato a "Echo Dot 3 5695c4" (…, cifrato: sì, … entità)
INFO server HTTP su 0.0.0.0:8124 (audio, MCP su /mcp, API su /api)
INFO Echo Dot 3 5695c4: richiesta (Alexa)
INFO Echo Dot 3 5695c4: hai detto: 'che ore sono?'
INFO Echo Dot 3 5695c4: risposta: Sono le 21 e 15.
INFO Echo Dot 3 5695c4: fine risposta (1 frasi)
```

- **L'Echo deve raggiungere il container sulla porta 8124**: scarica da lì le risposte. Il firewall dell'Echo lascia
  passare tutta la rete locale, quindi basta che il container non lo blocchi.
- **Più Echo:** `ECHO_HOSTS=192.168.1.50,192.168.1.51`. Si accordano da soli su chi risponde (vedi sotto).
- Le chiavi stanno in `/var/lib/echo-bridge/state/keys/`. **Fanne un backup:** senza, il bridge non rientra e devi
  cancellare la chiave sull'Echo con il cavo o con `scripts/adb-wifi.sh`.

### Le funzioni native, senza Home Assistant

| Funzione | Come funziona con il bridge |
|---|---|
| **Cifratura** | chiave generata dal bridge al primo collegamento, come fa Home Assistant |
| **Ora e fuso orario** | il bridge li dà all'Echo (`TIMEZONE`, ed eventualmente `HOME_LATITUDE`/`HOME_LONGITUDE`) |
| **Risposte in streaming** | l'Echo scarica la risposta dal bridge e la suona mentre l'agente scrive ancora |
| **Domande di seguito** | se la risposta finisce con "?", l'Echo riascolta da solo, senza "Alexa" |
| **Sussurro** | il sensore dell'Echo con il modello originale di Amazon, se è installato; altrimenti la stima del bridge |
| **Timer** | li chiede l'agente con `set_timer`; l'anello mostra il tempo, alla fine suona; "Alexa, stop" o il tasto lo fermano |
| **Annunci** | `announce` (MCP) o `POST /api/announce`; con `listen=true` l'Echo poi ascolta la risposta |
| **Musica** | da un URL, anche YouTube: il bridge la converte, la mette in pausa per voce e annunci e la riprende ([D2](#d2-musica-dal-tool-sullecho)) |
| **Più Echo: risponde solo il più vicino** | gli Echo si scambiano la chiave della loro rete attraverso il bridge, come attraverso Home Assistant |
| **Drop In** tra Echo | `run_action` con `drop_in`; la chiamata va da Echo a Echo |
| **Impostazioni e sensori** | `echo_status` e `set_entity`: non disturbare, suoni, luce dell'anello, equalizzatore, wake word, aggiornamenti |
| **Tasti, wake word, Drop In come eventi** | nel log del bridge e, con `EVENT_WEBHOOK`, inoltrati come POST JSON |
| **Annunci Bluetooth** ("Connesso a …") | l'Echo li chiede e il bridge li pronuncia |
| **Aggiornamenti di hassmic** | canale scelto nella pagina impostazioni dell'Echo; installazione con `set_entity` sull'entità di aggiornamento |

Restano fuori il proxy Bluetooth (serve solo a Home Assistant) e l'abbinamento automatico con Music Assistant, che
passa da Home Assistant 2026.11: Music Assistant si abbina comunque a mano con il token dell'Echo.

### Tasti e anello, una volta in funzione

| Comando | Cosa succede |
|---|---|
| "Alexa" | anello blu, ascolta (suono di attivazione, che si toglie con `-E`) |
| Tasto azione (puntino) | come "Alexa"; durante una risposta la interrompe e ascolta di nuovo |
| "Alexa, stop" mentre parla | interrompe la risposta |
| Volume +/− | volume dell'Echo, gestito in locale |
| Mute (microfono barrato) | **interruttore hardware**, anello rosso, nessun audio esce |
| Tasto azione tenuto 10 s | reset di hassmic: cancella impostazioni **e reti Wi-Fi salvate** (si riconfigura dal telefono, vedi la [guida del progetto](https://github.com/Gamer92000/echo-dot-assist/blob/main/docs/GUIDE.md)). Lo sblocco resta. **Mai oltre 20 s**: parte il reset di fabbrica di Amazon |
| Anello | blu = ascolta, ciano = parla, rosso = mute, breve animazione d'errore = agente o STT in errore |

### Sussurro → risposta a volume basso

Se sussurri, anche dopo un "Alexa" detto a voce normale, la risposta esce attenuata di `WHISPER_GAIN_DB` (−12 dB di
default). Il volume impostato sull'Echo non cambia. Nel log compare:

```
INFO 192.168.1.50: hai detto: 'spegni la luce' (sussurrato, parlato con tono 3%: risposta a -12 dB)
```

- **Chi decide, in modalità ESPHome:** il sensore "Last request whispered" dell'Echo, con il modello originale di
  Amazon. Nel log: `sussurrato, sensore dell'Echo`. Il modello si scarica una volta da Amazon dalla pagina impostazioni
  dell'Echo ("Download from Amazon"): vedi la [guida del progetto](https://github.com/Gamer92000/echo-dot-assist/blob/main/docs/GUIDE.md#whisper-detection).
  Senza quel modello, e in modalità Wyoming, decide la stima del bridge.
- **La stima del bridge:** misura quanta parte del parlato ha un tono, cioè quanto vibrano le corde vocali. Nel
  sussurro non vibrano. Non usa il volume, perché l'Echo alza da solo il guadagno di chi parla piano.
- **Provato su voce sintetica:** con espeak-ng la voce normale ha il 58–78% di parlato con tono, il sussurro lo 0–6%.
  La soglia è al 20% (`WHISPER_RATIO`). Su voci vere potresti doverla ritoccare guardando il valore nel log.
- **La risposta non è sussurrata:** Piper non sa sussurrare, quindi è la stessa voce a volume più basso.
- **Wake word nella richiesta:** se attivi "Wake word in the request" nella pagina impostazioni di hassmic, anche
  "Alexa" detto a voce normale entra nel calcolo e un sussurro potrebbe non essere riconosciuto. Di default è spento.

---

## Parte D — I tuoi tool MCP e l'Echo

I tuoi tool MCP girano altrove (nel loro server MCP) e non parlano con l'Echo direttamente. Quando un tool deve far
sentire qualcosa sull'Echo chiama l'API REST del bridge: `http://<ip-echo-bridge>:8124/api/...`, JSON in ingresso e
in uscita.

- **Autenticazione:** se nel bridge hai impostato `API_TOKEN`, ogni chiamata vuole `Authorization: Bearer <token>`.
- **Quale Echo:** il campo `echo` accetta il nome dell'Echo (quello di `GET /api/echos`), il nome del nodo o l'IP.
  Senza `echo` il bridge usa l'Echo che ha ricevuto l'ultima richiesta vocale: di solito è giusto, perché il tool è
  stato avviato proprio da una richiesta fatta a quell'Echo. Se il tool lavora a lungo e in casa ci sono più Echo,
  conviene che l'agente gli passi il nome (lo trova con lo strumento MCP `list_echos`) e che il tool lo ricordi.
- **Errori:** risposta HTTP 400 con `{"error": "..."}`, per esempio con un Echo sconosciuto o scollegato.

Negli esempi qui sotto:

```python
import asyncio, httpx

BRIDGE = "http://192.168.1.30:8124"                      # container echo-bridge
TOKEN = ""                                               # API_TOKEN del bridge, se impostato
AUTH = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
```

### D1. Avvisare l'Echo quando un tool finisce

Il tool fa il suo lavoro in background, risponde subito all'agente ("ok, ti avviso io") e alla fine chiama il bridge.
Ci sono due modi.

**Timer: il suono nativo dell'Echo (consigliato).** Se il tool gestisce un timer, lo registra anche sul bridge con la
stessa durata. Alla fine l'Echo suona come un timer di Alexa: l'anello mostra il tempo che resta, la suoneria va
avanti finché non dici "Alexa, stop" o premi il tasto.

| `POST /api/timers` | Campi | Risposta |
|---|---|---|
| avvio | `{"action": "start", "seconds": 600, "name": "pasta", "echo": "…"}` | `{"echo": "…", "timers": [{"id", "name", "total_seconds", "seconds_left", "active"}]}` |
| annullo | `{"action": "cancel", "name": "pasta"}` (oppure `"all": true`) | i timer annullati |
| pausa / ripresa | `{"action": "pause", "name": "pasta"}`, `{"action": "resume", "name": "pasta"}` | i timer cambiati |
| più o meno tempo | `{"action": "add", "name": "pasta", "seconds": 60}` (negativo per togliere) | i timer cambiati |
| elenco | `GET /api/timers?echo=…` | `{"echo": "…", "timers": [...]}` |

Senza `name`, annullo, pausa e ripresa agiscono sull'unico timer in corso. I timer vivono nella memoria del bridge: un
riavvio del bridge li cancella.

```python
async def timer_su_echo(secondi: int, nome: str = "", echo: str | None = None) -> dict:
    body = {"action": "start", "seconds": secondi, "name": nome, **({"echo": echo} if echo else {})}
    async with httpx.AsyncClient(timeout=10) as http:
        r = await http.post(f"{BRIDGE}/api/timers", json=body, headers=AUTH)
        r.raise_for_status()
        return r.json()
```

**Qualsiasi altro tool: un annuncio a voce.** A lavoro finito il tool fa dire una frase all'Echo.

| `POST /api/announce` | Campo | Significato |
|---|---|---|
| | `text` (obbligatorio) | la frase da dire |
| | `echo` | su quale Echo (vedi sopra) |
| | `listen` | `true`: dopo l'annuncio l'Echo ascolta la risposta e la manda all'agente come una normale richiesta, senza "Alexa" |
| risposta | `{"echo": "…", "played": true}` | `played: false` se l'Echo non l'ha suonato, per esempio con "non disturbare" attivo |

La chiamata ritorna quando l'Echo ha finito di parlare, al massimo dopo `ANNOUNCE_TIMEOUT` (120 s): usa un timeout
più lungo. Se c'è musica, si mette in pausa durante l'annuncio e poi riprende.

```python
_lavori = set()                                          # tiene vivi i task in background

async def avvisa_echo(testo: str, echo: str | None = None, ascolta: bool = False) -> bool:
    body = {"text": testo, "listen": ascolta, **({"echo": echo} if echo else {})}
    async with httpx.AsyncClient(timeout=150) as http:
        r = await http.post(f"{BRIDGE}/api/announce", json=body, headers=AUTH)
        r.raise_for_status()
        return r.json()["played"]

@mcp.tool()                                              # il tuo server MCP
async def scarica_backup(echo: str = "") -> str:
    async def lavoro():
        ...                                              # il lavoro vero e proprio
        await avvisa_echo("Il backup è finito.", echo or None)
    t = asyncio.create_task(lavoro()); _lavori.add(t); t.add_done_callback(_lavori.discard)
    return "Backup avviato: ti avviso sull'Echo quando ha finito."
```

Con `ascolta=True` l'avviso può diventare una domanda: "Il bucato è pronto, vuoi che te lo ricordi tra mezz'ora?".

### D2. Musica dal tool sull'Echo

Il tool continua a fare quello che fa oggi: cerca su YouTube con la sua libreria e ricava l'audio. Cambia solo
l'ultimo passaggio: invece di suonarlo su un altro dispositivo, passa al bridge l'URL dell'audio.

Perché passare dal bridge e non dare l'URL all'Echo direttamente:

- **Il firewall dell'Echo** lo lascia parlare solo con la rete locale, quindi non può scaricare da YouTube.
- **I formati:** l'Echo legge solo WAV e MP3, mentre YouTube dà Opus o AAC. Il bridge converte con ffmpeg.
- **Un URL alla volta:** hassmic suona un solo URL per volta. Se dici "Alexa" mentre suona, taglia la musica e non la
  riprende, e un annuncio che arriva mentre suona viene scartato. Il bridge invece mette in pausa la musica e la
  riprende dal punto in cui era.

| `POST /api/music` | Campi | Risposta |
|---|---|---|
| play | `{"action": "play", "url": "…", "headers": {…}, "title": "…", "seek": 0, "live": false, "echo": "…"}` | `{"echo", "state", "title", "url", "position", "live"}` |
| pause / resume / stop | `{"action": "pause"}`, `{"action": "resume"}`, `{"action": "stop"}` | lo stato aggiornato |
| stato | `GET /api/music?echo=…` | `state`: `playing`, `paused`, `interrupted` (in pausa per voce o annuncio, riprende da sola) o `idle` |

- `url`: l'URL diretto dell'audio, quello che la libreria ricava (con yt-dlp è `info["url"]`, non il link della pagina).
- `headers`: le intestazioni HTTP che la sorgente vuole (con yt-dlp `info["http_headers"]`); il bridge le passa a ffmpeg.
- `seek`: da quale secondo partire. `live: true` per radio e dirette, che non si possono riavvolgere.
- Un nuovo `play` sostituisce il brano in corso.

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
    body = {"action": "play", **audio, **({"echo": echo} if echo else {})}
    async with httpx.AsyncClient(timeout=15) as http:
        r = await http.post(f"{BRIDGE}/api/music", json=body, headers=AUTH)
        r.raise_for_status()
    return f"In riproduzione sull'Echo: {audio['title']}"

@mcp.tool()
async def musica(comando: str, echo: str = "") -> str:              # pause, resume, stop
    async with httpx.AsyncClient(timeout=15) as http:
        r = await http.post(f"{BRIDGE}/api/music", json={"action": comando, **({"echo": echo} if echo else {})}, headers=AUTH)
        r.raise_for_status()
        return r.json()["state"]
```

Cosa fa il bridge mentre la musica suona:

| Succede | Il bridge |
|---|---|
| Dici "Alexa" o premi il tasto | mette in pausa, ti ascolta e risponde, poi riprende dal punto in cui era |
| L'agente avvia la musica mentre sta ancora rispondendo ("Alexa, metti i Queen") | la tiene in coda e la fa partire a risposta finita, così la risposta non viene tagliata |
| Arriva un annuncio (D1) | pausa, annuncio, ripresa |
| Dici "Alexa, stop" | la mette in pausa e non la riprende da sola. Il tool può chiamare `resume` |
| Il tool chiama `pause` o `stop` (per esempio "Alexa, ferma la musica" all'agente) | pausa o stop. Dopo la risposta non riprende |
| Il brano finisce | stato `idle`; con `EVENT_WEBHOOK` arriva `{"type": "music", "event": "finished", "title": "…"}` |
| La sorgente non si apre (URL scaduto, 403) | stato `idle`; nel webhook `"event": "error"` con il messaggio di ffmpeg |

- **Coda e playlist:** le gestisce il tool. Al webhook `finished` (o controllando `GET /api/music` ogni tanto), il tool
  manda il `play` del brano successivo. Il webhook è quello di `EVENT_WEBHOOK` in `echo-bridge.env`: può essere un
  endpoint del server MCP stesso.
- **Volume:** strumento MCP `set_volume` del bridge, oppure i tasti dell'Echo.
- **URL di YouTube:** scadono dopo qualche ora e sono legati all'indirizzo IP pubblico di chi li ha ricavati. Il tool
  e il bridge dietro lo stesso router vanno bene. Se il tool esce da un'altra connessione (VPN), ffmpeg riceve 403: in
  quel caso il tool scarica il file e lo serve sulla rete locale via HTTP, e passa quell'URL al bridge.
- L'Echo Dot ha un solo altoparlante: il bridge manda un MP3 stereo a 192 kbit/s, e l'Echo lo riproduce in mono.

---

## Limiti noti

- **Limite di 30 s di hassmic:** l'Echo abbandona una richiesta dopo 30 s in "pensando". Se l'agente non ha ancora
  detto la prima frase dopo `KEEPALIVE_S` (20 s), il bridge comincia a mandare silenzio e l'anello passa a "parla". Nei
  test un agente da 35 s ha risposto regolarmente, in entrambe le modalità.
- **Timer in memoria:** se il bridge si riavvia, i timer in corso si perdono.
- **Un solo "Home Assistant" per Echo:** non collegare insieme Home Assistant e il bridge allo stesso Echo.
- **Solo modalità Wyoming:** collegamento in chiaro, niente timer, annunci, domande di seguito, sussurro nativo,
  arbitraggio, Drop In e strumenti MCP. Il tasto azione premuto mentre l'agente elabora ferma subito l'audio, ma il
  nuovo ascolto parte solo quando l'agente ha finito.
- **Aggiornamenti di hassmic in Wyoming:** solo dal PC con `scripts/ota-push.sh`. In ESPHome anche online, dall'entità
  di aggiornamento.

## Se qualcosa non va

| Sintomo | Dove guardare |
|---|---|
| `non raggiungibile` nel log del bridge | IP dell'Echo, `nc -zv <ip-echo> 26053` (16700 in Wyoming) |
| `l'Echo ha già una chiave che il bridge non conosce` | era in Home Assistant: vedi [B4](#b4-modalità-esphome-non-cè-niente-da-cambiare) |
| Risponde ma non si sente niente | l'Echo deve raggiungere il container sulla porta 8124; con più schede di rete imposta `PUBLIC_HOST` |
| L'agente non vede gli strumenti | indirizzo `http://<ip-echo-bridge>:8124/mcp` nel client MCP dell'agente, e `API_TOKEN` se impostato |
| Si accende ma `non hai detto niente` | Volume della voce o `VAD_MODE=1`; aumenta `NO_SPEECH_S` |
| Taglia la frase a metà | Aumenta `SILENCE_MS` (1000–1200) |
| Trascrizioni sbagliate | `WHISPER_MODEL=medium-int8` (rilancia `install.sh` oppure modifica `/etc/systemd/system/wyoming-whisper.service`) |
| Risposta lenta | `journalctl -u echo-bridge`: guarda dopo quanto arriva `risposta:`. Di solito è l'agente, quindi disattiva il "thinking" |
| Errore STT o TTS | `journalctl -u wyoming-whisper -u wyoming-piper` |
| Log dell'Echo | `adb shell cat /data/local/hassmic/boot.log` (USB, oppure `scripts/adb-wifi.sh <ip-echo>`) |
| Tornare ad Alexa originale | Vedi [Tornare ad Alexa originale](#tornare-ad-alexa-originale) |

---

## Tornare ad Alexa originale

Sì, si torna ad Alexa originale, e la parte software è semplice. Ci sono però due punti da sapere prima:

- **Lo sblocco resta.** Questi passaggi ripristinano il software di Amazon, ma non annullano lo sblocco del bootloader
  fatto nella parte B. L'Echo funziona come un Alexa normale, ma non è identico a uno nuovo di fabbrica.
- **Gli aggiornamenti di Amazon sono il punto di non ritorno.** Un Echo originale e online si aggiorna da solo, e un
  aggiornamento può chiudere la falla usata per lo sblocco. Dopo potresti non riuscire più a reinstallare hassmic.
  Per questo conviene partire dal livello 1, che blocca gli aggiornamenti.

Tutti i comandi si danno dal PC con la cartella `echo-dot-assist` usata per l'installazione e con
`secrets/update.key`. Se i fili USB sono staccati, apri adb via Wi-Fi per 30 minuti (funziona finché hassmic è attivo):

```bash
cd echo-dot-assist
scripts/adb-wifi.sh <ip-echo>
adb connect <ip-echo>:5555
```

### Livello 1 — Prova Alexa originale e poi torna indietro (consigliato)

L'Echo usa Alexa originale con internet e il cloud Amazon, quindi tornano anche funzioni come Voice ID e il sussurro
di Amazon. hassmic resta installato ma spento, e gli aggiornamenti firmware restano bloccati.

```bash
adb shell 'printf "MODE=stock-online\nADB_WIFI=1\n" >> /data/local/hassmic/hassmic.conf'
adb reboot
```

1. Sul router consenti all'Echo di uscire su internet e configuralo con l'app Alexa come un Echo nuovo.
2. `systemctl stop echo-bridge` nel container, perché in questa modalità non c'è niente a cui collegarsi.

`ADB_WIFI=1` serve perché con hassmic spento è l'unico modo di rientrare senza i fili USB. Però lascia una shell root
senza password aperta a tutta la rete locale: va bene per una prova, non per sempre.

**Per tornare al bridge:**

1. **Prima** rimuovi l'Echo dal tuo account nell'app Alexa, mentre è ancora online. Un Echo originale tolto
   dall'account mentre è online si resetta: cancella impostazioni e Wi-Fi di hassmic, ma non l'installazione.
2. Togli le due righe e riavvia:
   ```bash
   adb connect <ip-echo>:5555
   adb shell "sed -i '/^MODE=/d;/^ADB_WIFI=/d' /data/local/hassmic/hassmic.conf"
   adb shell cat /data/local/hassmic/hassmic.conf     # deve restare la riga ARGS (e PROTO=wyoming se lo usavi)
   adb reboot
   ```
3. Blocca di nuovo l'uscita su internet dal router e riavvia il bridge con `systemctl start echo-bridge`.

Se il reset al punto 1 ha cancellato il Wi-Fi, l'Echo mostra l'anello di configurazione. Ricollegalo dal telefono come
spiega la [guida del progetto](https://github.com/Gamer92000/echo-dot-assist/blob/main/docs/GUIDE.md) ("Setting up from a phone").

### Livello 2 — Disinstalla hassmic

Toglie hassmic dalla partizione di sistema e ripristina la policy SELinux originale, poi riavvia. Il risultato è
Fire OS 6574.1 originale, non registrato.

```bash
scripts/install-system.sh --uninstall       # al termine riavvia da solo
```

Dopo il riavvio adb via Wi-Fi non c'è più, perché hassmic non gira. I file rimasti in `/data/local/hassmic`
(impostazioni e aggiornamenti spinti) non vengono più usati e si possono lasciare. Per toglierli serve il cavo USB:
`adb shell rm -rf /data/local/hassmic`.

Poi lo configuri con l'app Alexa. **Da qui in poi l'Echo si aggiorna da solo**: tornare a hassmic potrebbe richiedere di
rifare lo sblocco, che dopo un aggiornamento potrebbe non funzionare più.

Esiste anche una via rapida e temporanea, `adb shell rm /data/local/hassmic/hassmic.conf` più riavvio. Ottieni lo
stesso Alexa originale con gli aggiornamenti sbloccati, ma i file di hassmic restano sul sistema.

### Livello 3 — Reinstalla tutto il firmware di Amazon

Se vuoi cancellare ogni traccia dal software, o l'Echo non si avvia più: avvia TWRP (`adb reboot recovery`, oppure
tieni premuto Volume + mentre colleghi l'alimentazione, con l'anello bianco) e reinstalla il firmware su entrambi gli
slot, come nella parte B. Serve il cavo USB.

```bash
F=firmware/donut/update-kindle-donut_puffin-NS65741_user_8138_0013222529668.bin
adb shell twrp wipe cache; adb shell twrp wipe data
adb push $F /sdcard/update.zip
adb shell twrp install /sdcard/update.zip
adb reboot recovery
adb shell twrp install /sdcard/update.zip      # deve indicare l'ALTRO slot
adb reboot
```

Anche qui lo sblocco resta. Per qualsiasi dubbio su questo livello il riferimento è la discussione XDA dello sblocco.

### Il lato server

Nel container echo-bridge:

```bash
systemctl disable --now echo-bridge wyoming-whisper wyoming-piper
```

In alternativa elimini il container da Proxmox. Non tocca nient'altro: llama.cpp e il server MCP restano come sono.

---

## Sviluppo e test

Il test end-to-end usa il simulatore PC di hassmic: compila `build/hassmic-host` dal progetto upstream, avvia
STT, TTS e agente finti ([`tests/fakes.py`](tests/fakes.py)) e simula "Alexa" e il tasto azione. La
[CI](.github/workflows/ci.yml) lo fa a ogni push. In locale, su Debian o Ubuntu:

```bash
sudo apt-get install -y build-essential libopus-dev zlib1g-dev espeak-ng python3-venv
git clone https://github.com/Gamer92000/echo-dot-assist.git /tmp/echo-dot-assist
make -C /tmp/echo-dot-assist build/hassmic-host
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
bash tests/ci.sh /tmp/echo-dot-assist/build/hassmic-host .venv            # modalità Wyoming
espeak-ng -v it -w voce.wav "Alexa, accendi la luce del soggiorno e dimmi che tempo fa domani"
.venv/bin/python tests/esphome_e2e.py /tmp/echo-dot-assist/build/hassmic-host voce.wav   # modalità ESPHome
```

## Crediti

- [echo-dot-assist / hassmic](https://github.com/Gamer92000/echo-dot-assist) di Gamer92000: sblocco guidato, firmware
  e protocolli ESPHome e Wyoming sull'Echo. Questo repository non contiene niente del loro codice e lo scarica solo per i test.
- [R0rt1z2](https://xdaforums.com/t/unlock-root-twrp-unbrick-amazon-echo-dot-3rd-gen-2018-donut.4801400/): exploit,
  TWRP e root dell'Echo Dot 3.
- [aioesphomeapi](https://github.com/esphome/aioesphomeapi) (il protocollo ESPHome, dalla libreria di Home Assistant),
  [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk).
- [Wyoming](https://github.com/OHF-Voice/wyoming), [wyoming-faster-whisper](https://github.com/rhasspy/wyoming-faster-whisper),
  [wyoming-piper](https://github.com/rhasspy/wyoming-piper).

