# ai-echo-dot-bridge

[![CI](https://github.com/andreadegiovine/ai-echo-dot-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/andreadegiovine/ai-echo-dot-bridge/actions/workflows/ci.yml)

**Echo Dot 3 → altoparlante del tuo agente locale, senza cloud e senza Home Assistant.**

L'Echo viene sbloccato con [echo-dot-assist (hassmic)](https://github.com/Gamer92000/echo-dot-assist) e messo in
modalità Wyoming. Questo repository contiene il bridge che lo collega a faster-whisper, Piper e al tuo agente
(llama.cpp + server MCP), tutto in un container LXC su Proxmox.

| File | Cosa contiene |
|---|---|
| [`echo_bridge.py`](echo_bridge.py) | il bridge Echo ↔ STT ↔ agente ↔ TTS |
| [`install.sh`](install.sh) | installazione nel container: librerie, utente e servizi systemd |
| [`echo-bridge.env`](echo-bridge.env) | configurazione di esempio (finisce in `/etc/echo-bridge/`) |
| [`selftest.py`](selftest.py) | autotest di STT, TTS e agente, senza Echo |
| [`tests/`](tests) | test end-to-end contro il simulatore di hassmic; li esegue la [CI](.github/workflows/ci.yml) |

## Come funziona

```
 Echo Dot 3 sbloccato (hassmic, PROTO=wyoming)          LXC "echo-bridge" su Proxmox
 ┌──────────────────────────────────────────┐         ┌──────────────────────────────────────┐
 │ wake word "Alexa" (motore Amazon, locale)│  TCP    │ echo_bridge.py                       │
 │ microfoni + cancellazione eco Amazon     │ 16700   │  ├─ VAD (fine frase)                 │
 │ tasti, mute hardware, anello LED         │◀───────▶│  ├─ faster-whisper :10300 (STT, it)  │
 │ altoparlante                             │         │  ├─ agente / llama.cpp (API OpenAI)  │──▶ LXC llama.cpp
 │ firewall: solo rete locale, mai Amazon   │         │  └─ Piper :10200 (TTS, it)           │
 └──────────────────────────────────────────┘         └──────────────────────────────────────┘
```

- **Sull'Echo** restano solo le parti che Amazon fa bene: wake word "Alexa", cancellazione dell'eco, tasti e LED.
  Il client Alexa è spento e un firewall impedisce qualsiasi contatto con Amazon.
- **Il container** collega l'Echo e fa tutto il resto: capisce quando hai finito di parlare, trascrive, chiede
  all'agente e legge la risposta. Le frasi vengono lette man mano che l'agente le scrive.
- **Nessun passthrough hardware:** l'Echo è in Wi-Fi e il container ci parla via rete. Basta un container non
  privilegiato.

Il bridge è provato end-to-end contro il simulatore di hassmic (`build/hassmic-host` del progetto upstream),
con STT, TTS e agente finti. La [CI](https://github.com/andreadegiovine/ai-echo-dot-bridge/actions/workflows/ci.yml)
ripete il test a ogni push. Il test ha coperto wake word, fine frase, trascrizione, risposta in streaming,
agente lento oltre i 30 s, sussurro con risposta attenuata, tasto azione e "non hai detto niente". **Non è stato provato su un Echo
vero, né con Whisper e Piper veri**: la prima prova reale sarà la tua.

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
| `wyoming` 1.11.0 | protocollo per parlare con l'Echo, Whisper e Piper |
| `wyoming-faster-whisper` 3.9.1 | server STT (faster-whisper / CTranslate2) |
| `wyoming-piper` 2.5.4 | server TTS (Piper) |
| `webrtcvad-wheels` 2.0.14 | rileva quando hai smesso di parlare |
| `httpx` 0.28.1 | chiamate in streaming all'agente |
| `numpy` 2.5.5 | riconoscimento del sussurro e attenuazione dell'audio |

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
    [`echo_bridge.py`](echo_bridge.py): deve restituire il testo a pezzi. Tutto il resto non cambia.
- `LLM_EXTRA_JSON`: per i modelli "thinking" (Qwen3 e simili) conviene disattivare il ragionamento, altrimenti
  l'Echo resta in silenzio a lungo. Il bridge comunque scarta `<think>…</think>` e `reasoning_content`.
- Prompt di sistema, contesto e strumenti li gestisce llama.cpp: il bridge manda solo la domanda trascritta, come
  unico messaggio `user`. Dalla risposta toglie comunque `<think>`, `*`, `#`, URL ed emoji, perché viene letta ad alta voce.

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

### B4. Passa l'Echo in modalità Wyoming (senza Home Assistant)

Di default hassmic parla ESPHome per Home Assistant. Il bridge usa invece il protocollo **Wyoming** (porta 16700).
Con il **cavo USB ancora collegato**:

```bash
adb shell cat /data/local/hassmic/hassmic.conf          # c'è solo ARGS=""
adb shell 'echo PROTO=wyoming >> /data/local/hassmic/hassmic.conf'
adb reboot
# dopo circa un minuto:
adb shell 'tail -n 30 /data/local/hassmic/boot.log' | grep -i wyoming
#   ... hassmic ... wyoming on 16700, wake=local
```

`wake=local` è fondamentale: significa che "Alexa" è riconosciuta dall'Echo. **Non** aggiungere `-w remote` agli ARGS:
il bridge non fa da server per la wake word.

Opzioni utili in `ARGS` (modifica il file e riavvia):

- `-E`: nessun suono quando senti "Alexa".
- `-a 0 -i 0`: niente arbitraggio né Drop In tra più Echo (in Wyoming non servono).

### B5. Prima di staccare i fili

- **Fai il backup di `secrets/update.key`.** Firma gli aggiornamenti di hassmic ed è l'unico modo di avere adb via
  Wi-Fi senza Home Assistant (`scripts/adb-wifi.sh <ip-echo>`, aperto 30 minuti).
- Controlla dal container che la porta sia aperta: `nc -zv <ip-echo> 16700`.
- Solo dopo dissalda, richiudi e riavvia.

---

## Parte C — Accendi tutto

```bash
# nel container echo-bridge
nano /etc/echo-bridge/echo-bridge.env        # ECHO_HOSTS=<ip-echo>
systemctl enable --now echo-bridge
journalctl -fu echo-bridge
```

Di' **"Alexa, che ore sono?"**. Nel log dovresti vedere:

```
INFO 192.168.1.50: collegato
INFO 192.168.1.50: richiesta (wake word: alexa)
INFO 192.168.1.50: hai detto: 'che ore sono?'
INFO HTTP Request: POST http://.../v1/chat/completions "HTTP/1.1 200 OK"
INFO 192.168.1.50: risposta: Sono le 21 e 15.
```

Con più Echo: `ECHO_HOSTS=192.168.1.50,192.168.1.51`.

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

- **Come lo riconosce:** misura quanta parte del parlato ha un tono, cioè quanto vibrano le corde vocali. Nel sussurro
  non vibrano. Non usa il volume, perché l'Echo alza da solo il guadagno di chi parla piano.
- **Provato su voce sintetica:** con espeak-ng la voce normale ha il 58–78% di parlato con tono, il sussurro lo 0–6%.
  La soglia è al 20% (`WHISPER_RATIO`). Su voci vere potresti doverla ritoccare guardando il valore nel log.
- **Non è il sussurro di Amazon:** il modello originale si scarica solo da Amazon e in modalità Wyoming non arriva al
  bridge. Piper non sa sussurrare, quindi la risposta è la stessa voce a volume più basso.
- **Wake word nella richiesta:** se attivi "Wake word in the request" nella pagina impostazioni di hassmic, anche
  "Alexa" detto a voce normale entra nel calcolo e un sussurro potrebbe non essere riconosciuto. Di default è spento.

---

## Limiti noti (modalità Wyoming)

- **Limite di 30 s di hassmic:** l'Echo abbandona una richiesta dopo 30 s in "pensando". Se l'agente non ha ancora
  detto la prima frase dopo `KEEPALIVE_S` (20 s), il bridge manda silenzio e l'anello passa a "parla". Nel test un
  agente da 35 s ha risposto regolarmente.
- **Tasto azione mentre l'agente sta ancora elaborando:** l'audio si ferma subito, ma il nuovo ascolto parte solo quando
  l'agente ha finito la risposta in corso. Con risposte normali non te ne accorgi.
- **Mancano rispetto a ESPHome:** timer, annunci, domande di seguito senza ridire "Alexa", Drop In, "risponde solo
  l'Echo più vicino" (con più Echo nella stessa stanza rispondono tutti) e aggiornamenti online. Gli aggiornamenti di
  hassmic si spingono dal PC con `scripts/ota-push.sh`.
- **Collegamento in chiaro:** Wyoming è TCP non cifrato. Tieni Echo e container su una rete o VLAN fidata.
- **Un solo client per Echo:** non collegare contemporaneamente Home Assistant e il bridge allo stesso Echo.

## Se qualcosa non va

| Sintomo | Dove guardare |
|---|---|
| `non raggiungibile` nel log del bridge | IP dell'Echo, `nc -zv <ip-echo> 16700`, `PROTO=wyoming` presente nel conf |
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
   adb shell cat /data/local/hassmic/hassmic.conf     # devono restare ARGS e PROTO=wyoming
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
bash tests/ci.sh /tmp/echo-dot-assist/build/hassmic-host .venv
```

## Crediti

- [echo-dot-assist / hassmic](https://github.com/Gamer92000/echo-dot-assist) di Gamer92000: sblocco guidato, firmware
  e protocollo Wyoming sull'Echo. Questo repository non contiene niente del loro codice e lo scarica solo per i test.
- [R0rt1z2](https://xdaforums.com/t/unlock-root-twrp-unbrick-amazon-echo-dot-3rd-gen-2018-donut.4801400/): exploit,
  TWRP e root dell'Echo Dot 3.
- [Wyoming](https://github.com/OHF-Voice/wyoming), [wyoming-faster-whisper](https://github.com/rhasspy/wyoming-faster-whisper),
  [wyoming-piper](https://github.com/rhasspy/wyoming-piper).

