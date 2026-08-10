#!/bin/sh
# Startet als root, richtet die Volumes ein und gibt die Rechte danach ab.
# So funktioniert ein frischer Bind-Mount (der dem Host-root gehoert), ohne dass
# jemand vorher von Hand chownen muss.
set -e

PUID="${PUID:-10001}"
PGID="${PGID:-10001}"
DATA_DIR="${APP_DATA_DIR:-/data}"
OUT_DIR="${SCAN_OUTPUT_DIR:-/scans}"

if [ "$(id -u)" != "0" ]; then
    # Bereits unprivilegiert gestartet (z. B. per "user:" in Compose):
    # nichts anzupassen, direkt weiter.
    exec "$@"
fi

if [ "$(id -u scanner)" != "$PUID" ]; then
    usermod -o -u "$PUID" scanner
fi
if [ "$(id -g scanner)" != "$PGID" ]; then
    groupmod -o -g "$PGID" scanner
fi

for dir in "$DATA_DIR" "$OUT_DIR"; do
    mkdir -p "$dir" 2>/dev/null || true
    if [ ! -d "$dir" ]; then
        echo "ScanDeck: $dir konnte nicht angelegt werden." >&2
        continue
    fi
    # Nur anfassen, was noch nicht passt: spart Zeit bei vielen Scans.
    if [ "$(stat -c %u "$dir")" != "$PUID" ] || [ "$(stat -c %g "$dir")" != "$PGID" ]; then
        chown -R "$PUID:$PGID" "$dir" 2>/dev/null \
            || echo "ScanDeck: Besitzer von $dir liess sich nicht setzen; bitte Rechte auf dem Host pruefen." >&2
    fi
    if ! su scanner -s /bin/sh -c "test -w '$dir'"; then
        echo "ScanDeck: $dir ist fuer den Dienst nicht beschreibbar (UID $PUID)." >&2
    fi
done

exec setpriv --reuid="$PUID" --regid="$PGID" --init-groups --inh-caps=-all "$@"
