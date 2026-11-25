#!/usr/bin/env python3
"""Test end-to-end della modalità ESPHome: build/hassmic-host (Echo simulato, -P esphome) <-> echobridge <-> server
finti (tests/fakes.py), più il server MCP del bridge. Uso: esphome_e2e.py <hassmic-host> <wav voce>"""
import asyncio, os, shutil, signal, subprocess, sys, tempfile, time, wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOST_BIN, VOICE_WAV = sys.argv[1], sys.argv[2]
FAILED = []


def check(ok, what):
    print(("ok   " if ok else "FAIL ") + what, flush=True)
    if not ok:
        FAILED.append(what)


def cap_file(path):
    w = wave.open(VOICE_WAV); r = w.getframerate()
    x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32)
    x = np.interp(np.linspace(0, len(x) - 1, int(len(x) * 16000 / r)), np.arange(len(x)), x)
    x = x / np.sqrt(np.mean(x[np.abs(x) > 200] ** 2)) * 32768 * 10 ** (-25 / 20)
    x = np.concatenate([np.zeros(16000), x, np.zeros(int(2.4 * 16000))])
    x += np.random.default_rng(0).normal(0, 32768 * 10 ** (-55 / 20), len(x))
    open(path, "wb").write(np.clip(x, -32768, 32767).astype("<i2").tobytes())


def read(p):
    try:
        return open(p, errors="replace").read()
    except OSError:
        return ""


async def wait_for(cond, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.1)
    return cond()


def peak(path, start=0):
    d = np.fromfile(path, dtype="<i2")[start // 2:]
    return int(np.abs(d).max()) if d.size else 0


class Setup:
    def __init__(self, port, http_port, whisper_model):
        self.dir = tempfile.mkdtemp()
        self.port, self.http_port = port, http_port
        st = os.path.join(self.dir, "echo-state"); os.makedirs(st)
        open(os.path.join(st, "config"), "w").write("even_volume=off\n")
        self.echo_state = st
        cap_file(os.path.join(self.dir, "cap.raw"))
        self.play = os.path.join(self.dir, "play.raw"); open(self.play, "wb").close()
        self.echo_log, self.bridge_log, self.fakes_log = (os.path.join(self.dir, n) for n in ("echo.log", "bridge.log", "fakes.log"))
        env = dict(os.environ, HASSMIC_STATE=st, HASSMIC_CAP=os.path.join(self.dir, "cap.raw"), HASSMIC_PLAY=self.play,
                   HASSMIC_MDNS_FILE=os.path.join(st, "hassmic.service"), HASSMIC_ARB_ADDR="127.255.255.255",
                   HASSMIC_FAKE_ADDRS=os.path.join(st, "addrs"), HASSMIC_FAKE_COUNTRY=os.path.join(st, "country"),
                   HASSMIC_CLOCK_SYNCED="0", HASSMIC_MODELS=os.path.join(st, "models"))
        if whisper_model:
            env["HASSMIC_FAKE_WHISPER"] = "1"                   # modello Amazon presente, ogni richiesta sussurrata
        open(env["HASSMIC_FAKE_ADDRS"], "w").write("127.0.0.1\n")
        self.echo_env = env
        self.procs = {}
        self.keys = os.path.join(self.dir, "keypad"); os.mkfifo(self.keys)
        self.keypad = os.open(self.keys, os.O_RDWR)             # tastiera finta (-b): eventi input scritti dal test

    def key(self, code, value):
        import struct
        os.write(self.keypad, struct.pack("llHHi", 0, 0, 1, code, value))

    def start_echo(self, name="Echo Dot", extra=(), env=None):
        self.procs["echo"] = subprocess.Popen([HOST_BIN, "-P", "esphome", "-p", str(self.port), "-n", name, "-W", "0", "-b", self.keys, *extra],
                                              env={**self.echo_env, **(env or {})}, stderr=open(self.echo_log, "a"))

    def start_fakes(self, slow=0):
        self.procs["fakes"] = subprocess.Popen([sys.executable, os.path.join(HERE, "fakes.py"), self.fakes_log, str(slow), "followup"],
                                               stderr=subprocess.DEVNULL)

    def start_bridge(self, hosts=None, **extra):
        env = dict(os.environ, ECHO_HOSTS=hosts or f"127.0.0.1:{self.port}", STATE_DIR=os.path.join(self.dir, "bridge-state"),
                   HTTP_PORT=str(self.http_port), HTTP_HOST="127.0.0.1", PUBLIC_HOST="127.0.0.1",
                   LLM_URL="http://127.0.0.1:18080/v1/chat/completions", TIMEZONE="Europe/Rome", **extra)
        self.procs["bridge"] = subprocess.Popen([sys.executable, "-m", "echobridge"], cwd=ROOT, env=env,
                                                stderr=open(self.bridge_log, "a"))

    def stop(self, name):
        p = self.procs.pop(name, None)
        if p:
            p.kill(); p.wait()

    def close(self):
        for n in list(self.procs):
            self.stop(n)
        try:
            os.close(self.keypad)
        except OSError:
            pass

    def wake(self):
        self.procs["echo"].send_signal(signal.SIGUSR1)


async def mcp_call(port, tool, args=None):
    from mcp.client import Client
    async with Client(f"http://127.0.0.1:{port}/mcp") as cl:
        r = await cl.call_tool(tool, args or {})
        sc = getattr(r, "structured_content", None)
        if sc is not None:
            return sc
        text = "".join(getattr(x, "text", "") for x in r.content)
        try:
            import json
            v = json.loads(text)
            return v
        except ValueError:
            return text


async def phase_a():
    print("== A. chiave, ora, voce, domanda di seguito, MCP (timer, annunci, volume, entità)")
    s = Setup(16971, 18124, whisper_model=False)
    try:
        hooks = []
        async def hook(reader, writer):
            head = await reader.readuntil(b"\r\n\r\n")
            n = next((int(l.split(b":")[1]) for l in head.split(b"\r\n") if l.lower().startswith(b"content-length")), 0)
            hooks.append((await reader.readexactly(n)).decode(errors="replace"))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"); await writer.drain(); writer.close()
        hook_srv = await asyncio.start_server(hook, "127.0.0.1", 18199)
        s.start_fakes(); s.start_echo(); await asyncio.sleep(1.5); s.start_bridge(EVENT_WEBHOOK="http://127.0.0.1:18199/hook")
        ok = await wait_for(lambda: "collegato a \"Echo Dot\"" in read(s.bridge_log))
        check(ok, "il bridge si collega all'Echo")
        check("chiave di cifratura impostata" in read(s.bridge_log) and "cifrato: sì" in read(s.bridge_log),
              "chiave di cifratura generata, impostata e usata")
        check(os.path.exists(os.path.join(s.echo_state, "api_key")), "l'Echo ha salvato la chiave (state/api_key)")
        check(await wait_for(lambda: "clock: Home Assistant's time handed to root" in read(s.echo_log), 10),
              "l'Echo ha preso l'ora dal bridge")

        before = os.path.getsize(s.play)
        s.wake()
        check(await wait_for(lambda: "fine risposta (2 frasi, riascolta)" in read(s.bridge_log), 25),
              "risposta che finisce con una domanda: l'Echo riascolterà")
        log = read(s.bridge_log)
        check("hai detto: 'che tempo fa domani?'" in log and "sussurrato" not in log, "trascrizione, voce normale")
        check(await wait_for(lambda: os.path.getsize(s.play) > before, 10), "risposta scaricata e suonata dall'Echo")
        a_peak = peak(s.play, before)
        check(await wait_for(lambda: "fine risposta (1 frasi)" in read(s.bridge_log), 30),
              "domanda di seguito senza \"Alexa\": seconda richiesta e risposta")
        check(read(s.bridge_log).count(": richiesta (") == 2, "la seconda richiesta l'ha aperta l'Echo da solo (un solo \"Alexa\")")
        llm = [l for l in read(s.fakes_log).splitlines() if l.startswith("llm:")]
        check(len(llm) >= 2 and llm[0].count('"role"') == 1 and llm[1].count('"role"') == 3,
              "domanda di seguito con il contesto della domanda prima (3 messaggi), la prima con uno solo")
        check(all('"role": "system"' not in l for l in llm), "nessun system prompt")

        echos = (await mcp_call(s.http_port, "list_echos"))["echos"]
        check(isinstance(echos, list) and echos and echos[0]["name"] == "Echo Dot" and echos[0]["connected"],
              f"MCP list_echos: {echos[0]['name'] if echos else echos}")
        st = await mcp_call(s.http_port, "echo_status")
        ids = {e["id"] for e in st.get("entities", [])}
        check({"do_not_disturb", "mute", "wake_sound"} <= ids, f"MCP echo_status: {len(ids)} entità, azioni {st.get('actions')}")

        t = await mcp_call(s.http_port, "set_timer", {"seconds": 3, "name": "tè"})
        check(isinstance(t, dict) and t["timers"][0]["name"] == "tè", f"MCP set_timer: {t}")
        check(await wait_for(lambda: "timer started: tè" in read(s.echo_log), 5), "l'Echo ha ricevuto il timer")
        lt = (await mcp_call(s.http_port, "list_timers"))["timers"]
        check(isinstance(lt, list) and len(lt) == 1, f"MCP list_timers: {lt}")
        check(await wait_for(lambda: "timer finished: tè" in read(s.echo_log), 8), "alla fine il timer suona sull'Echo")
        t2 = await mcp_call(s.http_port, "set_timer", {"seconds": 600, "name": "pasta"})
        await mcp_call(s.http_port, "pause_timer", {"name": "pasta"})
        check(await wait_for(lambda: "paused" in read(s.echo_log), 5), "timer in pausa")
        await mcp_call(s.http_port, "cancel_timer", {"name": "pasta"})
        check(await wait_for(lambda: "timer cancelled: pasta" in read(s.echo_log), 5), "timer annullato")
        import json as js, urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{s.http_port}/api/timers", headers={"Content-Type": "application/json"},
                                     data=js.dumps({"action": "start", "seconds": 2, "name": "dal tool"}).encode())
        r = js.loads(urllib.request.urlopen(req, timeout=10).read())
        check(r["timers"][0]["name"] == "dal tool", f"REST /api/timers: {r}")
        check(await wait_for(lambda: "timer finished: dal tool" in read(s.echo_log), 6), "timer creato da un tool via REST: suona sull'Echo")

        before = os.path.getsize(s.play)
        r = await mcp_call(s.http_port, "announce", {"text": "La lavatrice ha finito."})
        check(r.get("played") is True and os.path.getsize(s.play) > before, f"MCP announce: {r}")
        await mcp_call(s.http_port, "set_volume", {"level": 30})
        await asyncio.sleep(1)
        echos = (await mcp_call(s.http_port, "list_echos"))["echos"]
        check(abs(echos[0].get("volume", 0) - 0.3) < 0.02, f"MCP set_volume: volume {echos[0].get('volume')}")
        await mcp_call(s.http_port, "set_entity", {"entity": "do_not_disturb", "value": "on"})
        await asyncio.sleep(0.5)
        r = await mcp_call(s.http_port, "announce", {"text": "Non dovresti sentirmi."})
        check(r.get("played") is False, f"non disturbare blocca gli annunci: {r}")
        await mcp_call(s.http_port, "set_entity", {"entity": "do_not_disturb", "value": "off"})

        s.key(115, 1); await asyncio.sleep(0.15); s.key(115, 0)          # Volume + premuto e rilasciato
        check(await wait_for(lambda: "evento button:" in read(s.bridge_log), 5), "tasto Volume + arrivato come evento")
        check(await wait_for(lambda: any(__import__("json").loads(h or "{}").get("entity") == "button" for h in hooks), 5), f"evento inoltrato al webhook: {hooks[-1:] }")
        hook_srv.close()

        s.stop("bridge"); open(s.bridge_log, "w").close(); s.start_bridge()
        check(await wait_for(lambda: "collegato a \"Echo Dot\"" in read(s.bridge_log)), "riavvio del bridge: si ricollega")
        check("chiave di cifratura impostata" not in read(s.bridge_log) and "cifrato: sì" in read(s.bridge_log),
              "riavvio del bridge: usa la chiave salvata")
        return a_peak
    finally:
        s.close()
        if FAILED:
            print("--- bridge.log ---\n" + read(s.bridge_log)[-3000:])
            print("--- echo.log ---\n" + read(s.echo_log)[-3000:])
        shutil.rmtree(s.dir, ignore_errors=True)


async def phase_b(a_peak):
    print("== B. sussurro riconosciuto dal sensore nativo dell'Echo (modello Amazon)")
    s = Setup(16981, 18134, whisper_model=True)
    try:
        s.start_fakes(); s.start_echo(); await asyncio.sleep(1.5); s.start_bridge(FOLLOWUP="0")
        check(await wait_for(lambda: "collegato a \"Echo Dot\"" in read(s.bridge_log)), "collegato")
        before = os.path.getsize(s.play)
        s.wake()
        check(await wait_for(lambda: "fine risposta (2 frasi)" in read(s.bridge_log), 25), "risposta (FOLLOWUP=0: senza riascolto)")
        check("sussurrato, sensore dell'Echo" in read(s.bridge_log), "sussurro dal sensore \"Last request whispered\" dell'Echo")
        await wait_for(lambda: os.path.getsize(s.play) > before, 10); await asyncio.sleep(1.5)
        b_peak = peak(s.play, before)
        check(0 < b_peak < a_peak, f"risposta sussurrata più bassa: picco {b_peak} contro {a_peak}")
    finally:
        s.close()
        if FAILED:
            print("--- bridge.log ---\n" + read(s.bridge_log)[-3000:])
        shutil.rmtree(s.dir, ignore_errors=True)


def net(state):
    import re
    m = re.search(r"^net (\S+) ", read(os.path.join(state, "arbitration")), re.M)
    return m.group(1) if m else None


async def phase_c():
    print("== C. due Echo: entrano nella stessa rete di arbitraggio passando dal bridge (tag, come con Home Assistant)")
    arb = ["-a", "28990", "-z", "0", "-o", "0", "-L"]
    env = {"HASSMIC_ARB_SCALE": "0.1", "HASSMIC_KIOSK_PORT": "28991"}
    k, l = Setup(16991, 18144, False), Setup(16992, 18144, False)
    try:
        k.start_echo("Echo Cucina", arb, {**env, "HASSMIC_TEST_SCORE": "500"})
        l.start_echo("Echo Sala", arb, {**env, "HASSMIC_TEST_SCORE": "900"})
        await asyncio.sleep(1.5)
        k.start_bridge(hosts="127.0.0.1:16991,127.0.0.1:16992")
        check(await wait_for(lambda: read(k.bridge_log).count("collegato a") == 2), "il bridge si collega a entrambi")
        ok = await wait_for(lambda: net(k.echo_state) and net(k.echo_state) == net(l.echo_state), 40)
        both = read(k.echo_log) + read(l.echo_log)
        check(ok, f"stessa rete di arbitraggio: {net(k.echo_state)} / {net(l.echo_state)}")
        check("confirmed by Home Assistant" in both, "chiave passata tra Echo con la conferma del bridge (tag)")
    finally:
        k.close(); l.close()
        if FAILED:
            print("--- bridge.log ---\n" + read(k.bridge_log)[-2500:])
            print("--- cucina ---\n" + read(k.echo_log)[-2000:]); print("--- sala ---\n" + read(l.echo_log)[-2000:])
        shutil.rmtree(k.dir, ignore_errors=True); shutil.rmtree(l.dir, ignore_errors=True)


async def phase_d():
    print("== D. agente lento (35 s, oltre il limite di 30 s di hassmic)")
    s = Setup(16975, 18154, False)
    try:
        s.start_fakes(slow=35); s.start_echo(); await asyncio.sleep(1.5); s.start_bridge(FOLLOWUP="0")
        check(await wait_for(lambda: "collegato a" in read(s.bridge_log)), "collegato")
        s.wake()
        check(await wait_for(lambda: "fine risposta (2 frasi)" in read(s.bridge_log), 60), "risposta arrivata dopo 35 s")
        check("tengo viva la richiesta" in read(s.bridge_log), "keepalive attivato")
        check("pipeline timeout" not in read(s.echo_log), "l'Echo non ha abbandonato la richiesta")
    finally:
        s.close()
        if FAILED:
            print("--- bridge.log ---\n" + read(s.bridge_log)[-2500:]); print("--- echo ---\n" + read(s.echo_log)[-2000:])
        shutil.rmtree(s.dir, ignore_errors=True)


async def phase_e():
    print("== E. musica da un URL come quello di YouTube (Opus/WebM): conversione, pausa per voce e annunci, ripresa")
    import functools, http.server, json as js, threading, urllib.request
    www = tempfile.mkdtemp(); seen = []
    for name, secs in (("song.webm", 40), ("short.webm", 3)):
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=330:duration={secs}",
                        "-ac", "2", "-c:a", "libopus", "-b:a", "96k", os.path.join(www, name)], check=True)
    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k): super().__init__(*a, directory=www, **k)
        def do_GET(self): seen.append(self.headers.get("X-Test")); super().do_GET()
        def log_message(self, *a): pass
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 18310), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    s = Setup(16976, 18174, False)
    hooks = []
    async def hook(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        n = next((int(l.split(b":")[1]) for l in head.split(b"\r\n") if l.lower().startswith(b"content-length")), 0)
        hooks.append(js.loads(await reader.readexactly(n)))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"); await writer.drain(); writer.close()
    hook_srv = await asyncio.start_server(hook, "127.0.0.1", 18311)

    def api(body):
        req = urllib.request.Request(f"http://127.0.0.1:{s.http_port}/api/music", data=js.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        return js.loads(urllib.request.urlopen(req, timeout=10).read())
    blog = lambda: read(s.bridge_log)
    elog = lambda: read(s.echo_log)
    try:
        s.start_fakes(); s.start_echo(); await asyncio.sleep(1.5)
        s.start_bridge(FOLLOWUP="0", SENDSPIN="0", EVENT_WEBHOOK="http://127.0.0.1:18311/hook")
        check(await wait_for(lambda: "collegato a" in blog()), "collegato")
        st = api({"url": "http://127.0.0.1:18310/song.webm", "title": "Canzone", "headers": {"X-Test": "yt"}})
        check(st.get("state") == "playing", f"play: {st}")
        check(await wait_for(lambda: "media: MP3 44100 Hz x2" in elog(), 10), "l'Echo suona l'MP3 convertito dal bridge (sorgente Opus)")
        check(seen and seen[0] == "yt", f"gli headers della sorgente arrivano a ffmpeg: {seen}")

        await asyncio.sleep(4); s.wake()
        check(await wait_for(lambda: "musica interrotta per una richiesta" in blog(), 5), "\"Alexa\" mette in pausa la musica")
        check(await wait_for(lambda: "fine risposta" in blog(), 25), "la risposta arriva")
        check(await wait_for(lambda: blog().count('musica "Canzone" da ') >= 1, 15), "a risposta finita la musica riprende")
        import re
        pos = [int(x) for x in re.findall(r'musica "Canzone" da (\d+) s', blog())]
        check(pos and pos[0] >= 3, f"riprende dal punto in cui era: {pos} s")

        await asyncio.sleep(2)
        n = blog().count('musica "Canzone" da ')
        r = urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{s.http_port}/api/announce",
            data=js.dumps({"text": "La lavatrice ha finito."}).encode(), headers={"Content-Type": "application/json"}), timeout=60)
        check(js.loads(r.read()).get("played") is True, "annuncio durante la musica: riprodotto (non scartato)")
        check(await wait_for(lambda: blog().count('musica "Canzone" da ') > n, 10), "dopo l'annuncio la musica riprende")

        await asyncio.sleep(2); s.wake(); await asyncio.sleep(0.5)
        s.procs["echo"].send_signal(signal.SIGHUP)                       # "Alexa, stop"
        check(await wait_for(lambda: '"stop": musica in pausa' in blog(), 8), "\"Alexa, stop\": musica in pausa")
        n = blog().count('musica "Canzone" da '); await asyncio.sleep(5)
        check(blog().count('musica "Canzone" da ') == n and api({"action": "status"})["state"] == "paused",
              "dopo \"stop\" non riprende da sola")
        st = api({"action": "resume"})
        check(st["state"] == "playing" and await wait_for(lambda: blog().count('musica "Canzone" da ') > n, 5), f"resume dal tool: {st}")
        await asyncio.sleep(2)
        st = api({"action": "pause"}); await asyncio.sleep(4)
        check(st["state"] == "paused" and api({"action": "status"})["state"] == "paused", "pause dal tool: resta in pausa")
        st = api({"action": "stop"})
        check(st["state"] == "idle", f"stop dal tool: {st}")

        s.wake(); await asyncio.sleep(1.0)                               # l'agente avvia la musica mentre risponde
        st = api({"url": "http://127.0.0.1:18310/short.webm", "title": "Breve"})
        check(st["state"] == "interrupted" and "in coda, parte dopo la risposta" in blog(), f"play durante una richiesta: in coda ({st['state']})")
        check(await wait_for(lambda: 'musica "Breve"' in blog(), 30), "parte a risposta finita")
        check(await wait_for(lambda: "brano finito: Breve" in blog(), 20), "fine del brano riconosciuta")
        check(api({"action": "status"})["state"] == "idle", "dopo la fine: nessuna musica")
        check(await wait_for(lambda: any(h.get("type") == "music" and h.get("event") == "finished" and h.get("title") == "Breve"
                                         for h in hooks), 5), "webhook: evento \"finished\" per il tool musica")
        api({"url": "http://127.0.0.1:18310/manca.webm", "title": "Sparito"})
        check(await wait_for(lambda: "musica non riproducibile" in blog(), 15), "sorgente che non si apre: errore nel log")
        check(await wait_for(lambda: any(h.get("event") == "error" and h.get("title") == "Sparito" for h in hooks), 5),
              "webhook: evento \"error\"")
        check(api({"action": "status"})["state"] == "idle", "dopo l'errore: nessuna musica")
    finally:
        s.close(); httpd.shutdown(); hook_srv.close()
        if FAILED:
            print("--- bridge.log ---\n" + read(s.bridge_log)[-4000:]); print("--- echo ---\n" + read(s.echo_log)[-3000:])
        shutil.rmtree(s.dir, ignore_errors=True); shutil.rmtree(www, ignore_errors=True)


async def phase_f():
    print("== F. musica via Sendspin: l'Echo la abbassa per voce e annunci senza interromperla (come Alexa)")
    import functools, http.server, json as js, re, threading, urllib.request
    www = tempfile.mkdtemp()
    for name, secs in (("song.webm", 90), ("short.webm", 3)):
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=330:duration={secs}",
                        "-ac", "2", "-c:a", "libopus", "-b:a", "96k", os.path.join(www, name)], check=True)
    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k): super().__init__(*a, directory=www, **k)
        def log_message(self, *a): pass
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 18320), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    s = Setup(16977, 18184, False)
    music = os.path.join(s.dir, "music.raw")
    def api(path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{s.http_port}{path}", data=js.dumps(body).encode() if body else None,
                                     headers={"Content-Type": "application/json"})
        return js.loads(urllib.request.urlopen(req, timeout=60).read())
    blog, elog = (lambda: read(s.bridge_log)), (lambda: read(s.echo_log))
    msize = lambda: os.path.getsize(music) if os.path.exists(music) else 0
    def levels(start):                                   # RMS per 100 ms della musica scritta dall'Echo da start in poi
        d = np.fromfile(music, dtype="<i2")[start // 2:].astype(float)
        n = len(d) // 9600
        return np.sqrt((d[:n * 9600].reshape(n, 9600) ** 2).mean(axis=1)) if n else np.array([])
    def ducked(start):                                   # abbassata (sotto metà) ma mai muta, e poi di nuovo piena
        lv = levels(start)
        if len(lv) < 5: return False
        full = lv.max()
        return lv.min() > 0.02 * full and lv.min() < 0.5 * full and lv[-1] > 0.8 * full
    br = dict(FOLLOWUP="0", SENDSPIN_ECHO_PORT="16978", SENDSPIN_SERVER_PORT="18927")
    try:
        s.start_fakes(); s.start_echo(extra=("-z", "16978"), env={"HASSMIC_MUSIC": music, "HASSMIC_OUTPUT_LATENCY_MS": "0"})
        await asyncio.sleep(1.5); s.start_bridge(**br)
        check(await wait_for(lambda: "lettore Sendspin abbinato" in blog(), 25), "abbinato al lettore Sendspin con il token dell'Echo")
        st = api("/api/music", {"url": "http://127.0.0.1:18320/song.webm", "title": "Canzone"})
        check(st.get("backend") == "sendspin" and st["state"] == "playing", f"play via Sendspin: {st}")
        check(await wait_for(lambda: "sendspin: stream start" in elog() and msize() > 96000, 15), "l'Echo riceve la musica dal suo lettore Sendspin")

        await asyncio.sleep(2); m0 = msize(); s.wake()
        check(await wait_for(lambda: "fine risposta" in blog(), 25), "la risposta arriva")
        await asyncio.sleep(3)
        lv = levels(m0)
        print("     livelli dopo \"Alexa\" (ogni 0,5 s):", [int(x) for x in lv[::5]], flush=True)
        check(ducked(m0), f"\"Alexa\": musica abbassata (livello {lv.min():.0f} contro {lv.max():.0f}), mai muta, poi di nuovo piena")
        check("musica interrotta" not in blog() and api("/api/music")["state"] == "playing", "lo stato resta \"playing\"")

        await asyncio.sleep(1); m0 = msize()
        r = api("/api/announce", {"text": "La lavatrice ha finito."})
        check(r.get("played") is True, "annuncio sopra la musica: riprodotto")
        await asyncio.sleep(2)
        lv = levels(m0)
        check(ducked(m0) and api("/api/music")["state"] == "playing",
              f"durante l'annuncio la musica si abbassa e continua (livello {lv.min():.0f} contro {lv.max():.0f})")

        await asyncio.sleep(1); s.wake(); await asyncio.sleep(0.5); s.procs["echo"].send_signal(signal.SIGHUP)   # "Alexa, stop"
        check(await wait_for(lambda: "pausa dall'Echo" in blog() and api("/api/music")["state"] == "paused", 8), "\"Alexa, stop\": in pausa")
        n = blog().count("fine risposta") + blog().count("non hai detto niente")
        await wait_for(lambda: blog().count("fine risposta") + blog().count("non hai detto niente") > n, 25)   # la richiesta finisce
        await asyncio.sleep(2)
        s.procs["echo"].send_signal(signal.SIGUSR2)                              # tasto azione
        check(await wait_for(lambda: "ripresa dall'Echo" in blog() and api("/api/music")["state"] == "playing", 8),
              "il tasto azione la riprende")
        pos = api("/api/music")["position"]
        check(pos >= 3, f"riprende dal punto in cui era: {pos} s")
        check(api("/api/music", {"action": "stop"})["state"] == "idle", "stop dal tool")
        api("/api/music", {"url": "http://127.0.0.1:18320/short.webm", "title": "Breve"})
        check(await wait_for(lambda: "brano finito: Breve" in blog(), 20) and api("/api/music")["state"] == "idle",
              "fine del brano riconosciuta")
        api("/api/music", {"url": "http://127.0.0.1:18320/manca.webm", "title": "Sparito"})
        check(await wait_for(lambda: "musica non riproducibile" in blog(), 15) and api("/api/music")["state"] == "idle",
              "sorgente che non si apre: errore")

        s.stop("bridge"); open(s.bridge_log, "w").close(); s.start_bridge(**br)
        check(await wait_for(lambda: "lettore Sendspin collegato" in blog(), 25), "riavvio del bridge: abbinamento salvato, nessun nuovo abbinamento")
    finally:
        s.close(); httpd.shutdown()
        if FAILED:
            print("--- bridge.log ---\n" + read(s.bridge_log)[-4000:]); print("--- echo ---\n" + read(s.echo_log)[-4000:])
        shutil.rmtree(s.dir, ignore_errors=True); shutil.rmtree(www, ignore_errors=True)


async def phase_g():
    print("== G. sveglie, promemoria, salvataggio su disco, notifiche, \"tutti\", sleep della musica, modalità notte")
    import datetime as dtm, functools, http.server, json as js, threading, urllib.request
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Rome")
    www = tempfile.mkdtemp()
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=330:duration=60",
                    "-ac", "2", "-c:a", "libopus", "-b:a", "96k", os.path.join(www, "song.webm")], check=True)
    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k): super().__init__(*a, directory=www, **k)
        def log_message(self, *a): pass
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 18330), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    s = Setup(16979, 18194, False)
    def api(path, body=None, method=None):
        req = urllib.request.Request(f"http://127.0.0.1:{s.http_port}{path}", data=js.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"}, method=method)
        try:
            return js.loads(urllib.request.urlopen(req, timeout=90).read())
        except urllib.error.HTTPError as e:
            return {"http": e.code, **js.loads(e.read() or b"{}")}
    blog, elog = (lambda: read(s.bridge_log)), (lambda: read(s.echo_log))
    br = dict(FOLLOWUP="0", SENDSPIN="0")
    def ent(oid):
        st = api("/api/echos?entities=1")[0]
        return next((e.get("state", {}).get("state") for e in st["entities"] if e["id"] == oid), None)
    try:
        s.start_fakes(); s.start_echo(); await asyncio.sleep(1.5); s.start_bridge(**br)
        check(await wait_for(lambda: "collegato a" in blog()), "collegato")

        # sveglia al prossimo minuto, giorno preciso
        nxt = (dtm.datetime.now(TZ) + dtm.timedelta(minutes=1)).replace(second=0, microsecond=0)
        r = api("/api/alarms", {"action": "set", "time": nxt.strftime("%H:%M"), "date": nxt.date().isoformat(), "name": "Sveglia test"})
        check(r.get("alarms") and r["alarms"][0]["next"] == nxt.strftime("%Y-%m-%d %H:%M"), f"sveglia impostata: {r}")
        r = api("/api/alarms", {"action": "set", "time": "07:00", "days": "feriali", "name": "Lavoro"})
        nday = dtm.date.fromisoformat(r["alarms"][0]["next"][:10]) if r.get("alarms") else None
        check(nday is not None and nday.weekday() < 5 and r["alarms"][0]["days"] == [0, 1, 2, 3, 4], f"sveglia ricorrente nei feriali: {r}")
        check(api("/api/alarms", {"action": "set", "time": "25:00"}).get("http") == 400, "orario non valido: errore 400")

        # promemoria in 3 s, e uno durante "non disturbare"
        before = os.path.getsize(s.play)
        r = api("/api/reminders", {"text": "chiamare Marco", "in_minutes": 0.05})
        check(r.get("reminders"), f"promemoria impostato: {r}")
        check(await wait_for(lambda: "promemoria detto: chiamare Marco" in blog(), 15) and os.path.getsize(s.play) > before,
              "promemoria detto dall'Echo all'ora giusta")
        check(api("/api/reminders?action=list")["reminders"] == [], "promemoria singolo: tolto dopo averlo detto")
        await mcp_call(s.http_port, "set_entity", {"entity": "do_not_disturb", "value": "on"}); await asyncio.sleep(0.5)
        await mcp_call(s.http_port, "set_reminder", {"text": "bere acqua", "in_minutes": 0.05})
        await asyncio.sleep(6)
        check("promemoria detto: bere acqua" not in blog(), "con \"non disturbare\" il promemoria aspetta")
        await mcp_call(s.http_port, "set_entity", {"entity": "do_not_disturb", "value": "off"})
        check(await wait_for(lambda: "promemoria detto: bere acqua" in blog(), 15), "finito \"non disturbare\", il promemoria viene detto")

        # notifiche
        r = api("/api/notify", {"text": "Il backup è finito", "source": "backup"})
        check(len(r.get("notifications", [])) == 1, f"notifica lasciata: {r}")
        check(api("/api/echos")[0]["notifications"] == 1, "l'Echo ha 1 notifica in attesa")
        r = await mcp_call(s.http_port, "read_notifications")
        check(r.get("notifications") and r["notifications"][0]["text"] == "Il backup è finito" and r.get("cleared"),
              f"l'agente legge le notifiche: {r}")
        check(api("/api/notify?action=list")["notifications"] == [], "lette: nessuna notifica in attesa")

        # tutti
        r = api("/api/announce", {"text": "La cena è pronta", "echo": "all"})
        check(r.get("all") and r["results"][0]["played"] is True, f"annuncio a tutti: {r}")
        r = api("/api/timers", {"action": "start", "seconds": 600, "name": "pasta", "echo": "tutti"})
        check(r.get("all") and r["results"][0]["timers"][0]["name"] == "pasta", f"timer su tutti: {r}")

        # sveglia al minuto (impostata all'inizio)
        check(await wait_for(lambda: "timer finished: Sveglia test" in elog(), 75), "la sveglia suona all'ora impostata")
        check(all(a["name"] != "Sveglia test" for a in api("/api/alarms?action=list")["alarms"]), "sveglia singola: tolta dopo aver suonato")

        # salvataggio su disco: riavvio del bridge
        api("/api/reminders", {"text": "riunione", "in_minutes": 120})
        s.stop("bridge"); open(s.bridge_log, "w").close(); s.start_bridge(**br)
        check(await wait_for(lambda: "collegato a" in blog()), "riavvio del bridge")
        st = api("/api/echos")[0]
        check([t["name"] for t in st["timers"]] == ["pasta"] and [a["name"] for a in st["alarms"]] == ["Lavoro"]
              and [r["text"] for r in st["reminders"]] == ["riunione"], f"dopo il riavvio: timer, sveglie e promemoria ci sono ancora")
        n = elog().count("timer started: pasta")
        check(n >= 2, "il timer è di nuovo sull'Echo dopo il riavvio")
        api("/api/timers", {"action": "start", "seconds": 3, "name": "breve"})
        s.stop("bridge"); await asyncio.sleep(5); open(s.bridge_log, "w").close(); s.start_bridge(**br)
        check(await wait_for(lambda: "timer finished: breve" in elog(), 20), "timer scaduto a bridge spento: suona appena torna")
        api("/api/timers", {"action": "cancel", "all": True}); api("/api/alarms", {"action": "cancel", "all": True})
        api("/api/reminders", {"action": "cancel", "all": True})

        # sleep della musica (dal media player: SENDSPIN=0 qui)
        await mcp_call(s.http_port, "set_volume", {"level": 60}); await asyncio.sleep(0.5)
        api("/api/music", {"url": "http://127.0.0.1:18330/song.webm", "title": "Ninna nanna"})
        await asyncio.sleep(2)
        r = await mcp_call(s.http_port, "music_sleep", {"minutes": 0.1, "fade": 3})
        check(r.get("sleep") and r["sleep"]["seconds_left"] <= 6, f"sleep impostato: {r.get('sleep')}")
        check(await wait_for(lambda: "sleep: musica fermata" in blog(), 15), "allo scadere la musica si abbassa e si ferma")
        check("sleep: la musica si abbassa" in blog(), "prima si abbassa piano")
        await asyncio.sleep(1)
        e0 = api("/api/echos")[0]
        check(e0["music"]["state"] == "idle" and abs((e0.get("volume") or 0) - 0.6) < 0.02, f"musica ferma, volume di nuovo a 60: {e0.get('volume')}")

        # modalità notte: fascia che contiene adesso
        s.stop("bridge"); open(s.bridge_log, "w").close()
        now = dtm.datetime.now(TZ)
        s.start_bridge(**br, NIGHT_START=(now - dtm.timedelta(minutes=5)).strftime("%H:%M"),
                       NIGHT_END=(now + dtm.timedelta(minutes=30)).strftime("%H:%M"), NIGHT_VOLUME="15")
        check(await wait_for(lambda: "modalità notte attiva" in blog(), 15), "modalità notte attiva nella fascia")
        await asyncio.sleep(1)
        check(ent("do_not_disturb") is True and abs((api("/api/echos")[0].get("volume") or 0) - 0.15) < 0.02,
              "di notte: non disturbare acceso e volume a 15")
        r = api("/api/night", {"action": "off"})
        check(r.get("mode") == "off" and await wait_for(lambda: "modalità notte finita" in blog(), 5), f"spenta a mano: {r}")
        await asyncio.sleep(1)
        check(ent("do_not_disturb") is False and abs((api("/api/echos")[0].get("volume") or 0) - 0.6) < 0.02,
              "non disturbare spento e volume di nuovo a 60")
    finally:
        s.close(); httpd.shutdown()
        if FAILED:
            print("--- bridge.log ---\n" + read(s.bridge_log)[-4000:]); print("--- echo ---\n" + read(s.echo_log)[-3000:])
        shutil.rmtree(s.dir, ignore_errors=True); shutil.rmtree(www, ignore_errors=True)


async def main():
    if os.environ.get("ONLY"):
        await globals()["phase_" + os.environ["ONLY"]]()
        print("TUTTI I TEST ESPHOME OK" if not FAILED else f"FALLITI: {len(FAILED)}")
        sys.exit(1 if FAILED else 0)
    a_peak = await phase_a()
    await phase_b(a_peak)
    await phase_c()
    await phase_d()
    await phase_e()
    await phase_f()
    await phase_g()
    print("TUTTI I TEST ESPHOME OK" if not FAILED else f"FALLITI: {len(FAILED)}")
    sys.exit(1 if FAILED else 0)

asyncio.run(main())
