#!/bin/bash
# Test end-to-end: hassmic-host (Echo simulato, PROTO=wyoming) <-> echobridge <-> server finti.
# Uso: run_test.sh <percorso build/hassmic-host> <venv> [ritardo agente in s]
set -u
HOST_BIN=$1; VENV=$2; SLOW=${3:-0}
HERE=$(cd "$(dirname "$0")" && pwd); W=$(mktemp -d)
. "$VENV/bin/activate"
if [ -n "${CAP_WAV:-}" ]; then                             # voce vera (es. espeak-ng) al posto di quella sintetica
python3 - "$CAP_WAV" "$W/cap.raw" <<'EOF'
import sys, wave, numpy as np
w = wave.open(sys.argv[1]); r = w.getframerate()
x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32)
x = np.interp(np.linspace(0, len(x) - 1, int(len(x) * 16000 / r)), np.arange(len(x)), x)
x = x / np.sqrt(np.mean(x[np.abs(x) > 200] ** 2)) * 32768 * 10 ** (-25 / 20)
x = np.concatenate([np.zeros(16000), x, np.zeros(int(2.4 * 16000))])
x += np.random.default_rng(0).normal(0, 32768 * 10 ** (-55 / 20), len(x))
open(sys.argv[2], "wb").write(np.clip(x, -32768, 32767).astype("<i2").tobytes())
EOF
else
python3 - "$W/cap.raw" <<'EOF'
import math, random, struct, sys
r, out = 16000, []
out += [0] * r                                          # 1 s silenzio
for i in range(int(1.6 * r)):                           # 1.6 s di "voce" sintetica (vocale con armoniche)
    t = i / r; f0 = 140 + 20 * math.sin(2 * math.pi * 3 * t)
    v = sum(math.sin(2 * math.pi * f0 * k * t) / k for k in range(1, 12)) * (0.6 + 0.4 * math.sin(2 * math.pi * 4 * t))
    out.append(int(6000 * v + random.gauss(0, 200)))
out += [0] * (int(2.4 * r))                                 # 12 s silenzio
open(sys.argv[1], "wb").write(struct.pack(f"<{len(out)}h", *[max(-32768, min(32767, s)) for s in out]))
EOF
fi
mkdir -p "$W/state"; echo "even_volume=off" > "$W/state/config"
HASSMIC_CAP=$W/cap.raw HASSMIC_PLAY=$W/play.raw HASSMIC_FAKE_LED=$W/led HASSMIC_STATE=$W/state \
  "$HOST_BIN" -P wyoming -p 16799 -n "Test Dot" 2> "$W/hassmic.log" & ECHO_PID=$!
python3 "$HERE/fakes.py" "$W/fakes.log" "$SLOW" 2> "$W/fakes.err" & FAKES_PID=$!
sleep 2
(cd "$HERE/.." && ECHO_HOSTS=wyoming://127.0.0.1:16799 LLM_URL=http://127.0.0.1:18080/v1/chat/completions KEEPALIVE_S=${KEEPALIVE_S:-20} \
  exec python3 -m echobridge) 2> "$W/bridge.log" & BRIDGE_PID=$!
sleep 2
kill -USR1 $ECHO_PID                                     # "Alexa"
sleep ${WAIT:-10}
kill -USR1 $ECHO_PID                                     # seconda domanda
sleep ${WAIT:-10}
kill -9 $BRIDGE_PID $FAKES_PID $ECHO_PID 2>/dev/null; wait 2>/dev/null
echo "== bridge";  cat "$W/bridge.log"
echo "== fakes";   cat "$W/fakes.log"
echo "== hassmic (estratto)"; grep -E -i "satellite|client|transcript|state|play|error|run|wake" "$W/hassmic.log" | head -40
echo "== audio riprodotto: $(stat -c %s "$W/play.raw" 2>/dev/null || echo 0) byte, picco $(python3 -c 'import sys, numpy as n; d = n.fromfile(sys.argv[1], dtype="<i2"); print(int(n.abs(d).max()) if d.size else 0)' "$W/play.raw" 2>/dev/null)"
echo "dir: $W"
