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
        check(isinstance(t, dict) and t.get("name") == "tè", f"MCP set_timer: {t}")
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
        check("riprodotto" in str(r) and os.path.getsize(s.play) > before, f"MCP announce: {r}")
        await mcp_call(s.http_port, "set_volume", {"level": 30})
        await asyncio.sleep(1)
        echos = (await mcp_call(s.http_port, "list_echos"))["echos"]
        check(abs(echos[0].get("volume", 0) - 0.3) < 0.02, f"MCP set_volume: volume {echos[0].get('volume')}")
        await mcp_call(s.http_port, "set_entity", {"entity": "do_not_disturb", "value": "on"})
        await asyncio.sleep(0.5)
        r = await mcp_call(s.http_port, "announce", {"text": "Non dovresti sentirmi."})
        check("non riprodotto" in str(r), f"non disturbare blocca gli annunci: {r}")
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
        s.start_bridge(FOLLOWUP="0", EVENT_WEBHOOK="http://127.0.0.1:18311/hook")
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
    print("TUTTI I TEST ESPHOME OK" if not FAILED else f"FALLITI: {len(FAILED)}")
    sys.exit(1 if FAILED else 0)

asyncio.run(main())
