#!/bin/bash
# Scenari end-to-end con verifiche: esce con errore se uno fallisce.
# Uso: tests/ci.sh <percorso build/hassmic-host> <venv>
set -u
HOST_BIN=$1; VENV=$2
HERE=$(cd "$(dirname "$0")" && pwd)
WAV=$(mktemp -d); FAIL=0

check() {   # check <descrizione> <output> <regex> [conteggio minimo]
    local n; n=$(grep -E -c -- "$3" <<<"$2")
    if [ "$n" -ge "${4:-1}" ]; then echo "ok   $1"; else echo "FAIL $1 (atteso '$3' x${4:-1}, trovato $n)"; FAIL=1; fi
}
refute() {
    if grep -E -q -- "$3" <<<"$2"; then echo "FAIL $1 (trovato '$3')"; FAIL=1; else echo "ok   $1"; fi
}
run() { timeout 150 bash "$HERE/run_test.sh" "$HOST_BIN" "$VENV" "$@" 2>&1; pkill -9 -f "hassmic-host -P wyoming|fakes.py .*fakes.log|python3 .*echo_bridge.py" 2>/dev/null; true; }
peak() { sed -n 's/.*picco \([0-9]*\).*/\1/p' <<<"$1"; }

echo "== 1. voce sintetica, due richieste"
out=$(WAIT=9 run)
check "due richieste ricevute" "$out" "richiesta \(wake word: alexa\)" 2
check "trascrizione" "$out" "hai detto: 'che tempo fa domani\?'" 2
check "solo il messaggio user all'agente" "$out" 'llm: messages=\[\{"role": "user", "content": "che tempo fa domani\?"\}\]' 2
check "<think> e markdown tolti" "$out" "risposta: Domani a Roma sole e 22 gradi\." 2
check "anello: pensa -> parla -> riposo" "$out" "state: speaking -> idle" 2
refute "nessun errore" "$out" "Traceback|ERROR"
echo "$out" | grep -E "riprodotto"

espeak-ng -v it -s 150 -w "$WAV/voce.wav" "Alexa, accendi la luce del soggiorno e dimmi che tempo fa domani"
espeak-ng -v it+whisper -s 150 -w "$WAV/sussurro.wav" "Alexa, accendi la luce del soggiorno e dimmi che tempo fa domani"

echo "== 2. voce normale (espeak-ng)"
out=$(CAP_WAV=$WAV/voce.wav WAIT=9 run)
check "risposta" "$out" "fine risposta" 2
refute "non preso per sussurro" "$out" "sussurrato"
p_voce=$(peak "$out"); echo "     picco riprodotto $p_voce"

echo "== 3. sussurro (espeak-ng)"
out=$(CAP_WAV=$WAV/sussurro.wav WAIT=9 run)
check "sussurro riconosciuto" "$out" "sussurrato, parlato con tono" 2
p_sus=$(peak "$out"); echo "     picco riprodotto $p_sus"
if [ -n "$p_voce" ] && [ -n "$p_sus" ] && [ "$p_sus" -gt 0 ] && [ "$p_sus" -lt "$p_voce" ]; then
    echo "ok   risposta attenuata ($p_sus < $p_voce)"; else echo "FAIL risposta non attenuata ($p_sus vs $p_voce)"; FAIL=1; fi

echo "== 4. agente lento (35 s, oltre il limite di 30 s di hassmic)"
out=$(WAIT=42 run 35)
check "keepalive attivato" "$out" "tengo viva la richiesta" 2
check "risposta arrivata comunque" "$out" "fine risposta \(2 frasi\)" 2
refute "nessun errore" "$out" "Traceback|ERROR"

rm -rf "$WAV"
[ $FAIL = 0 ] && echo "TUTTI I TEST OK" || echo "ALCUNI TEST FALLITI"
exit $FAIL
