# ScanDeck

Eine lokale, mobil-optimierte Weboberfläche (PWA) für einen eSCL-fähigen Netzwerk-Scanner. Sie startet Scans, zeigt den Fortschritt live an, blendet nach dem Scan kurz eine Vorschau ein, speichert eine lokale Kopie und übergibt diese an Paperless-ngx. Über eine geschützte REST-Schnittstelle kann Home Assistant Scans auslösen.

| Dashboard | Scanvorgang |
| --- | --- |
| ![Dashboard](assets/mainpage.png) | ![Scanfortschritt](assets/scan.png) |

## Starten

```powershell
docker compose up --build -d
```

Danach im Browser öffnen: `http://localhost:8080`.

Beim ersten Start ist **nichts vorkonfiguriert** — es startet automatisch ein Einrichtungsassistent, der Scanner, Paperless-ngx, Ausgabeformat und Automatisierung abfragt. Erst danach ist das Dashboard nutzbar. Einstellungen landen in `./data/config.json`, Scans in `./scans`; beides sind Volumes.

```powershell
docker compose down
```

## Als App aufs Handy legen (PWA)

Die Oberfläche ist installierbar: In Chrome/Android über *Zum Startbildschirm hinzufügen*, in Safari/iOS über *Teilen → Zum Home-Bildschirm*. Sie läuft dann im Vollbild ohne Browserleiste, respektiert die Safe-Area und funktioniert offline so weit, dass die Oberfläche lädt (Scans brauchen natürlich das Netzwerk). Schnellaktionen im App-Icon: *Sofort scannen* und *Einstellungen*.

Damit iOS die App installieren lässt, muss die Seite über HTTPS oder `localhost` erreichbar sein — im LAN also am besten hinter einem Reverse Proxy mit Zertifikat.

## Bedienung

**Dashboard** ist auf den täglichen Ablauf reduziert: großer Scan-Button mit Fortschrittsring, vier antippbare Schnellschalter (Quelle, Format, DPI, Farbe — jeder Tipp schaltet weiter und speichert sofort), Session-Tags nur für den nächsten Scan, Statuskacheln und ein einklappbares Live-Protokoll.

Während des Scans erscheint eine Fortschrittsanzeige mit Prozentwert, Laufzeit und Phasen (Verbindung → Erfassen → Speichern → Upload). Anschließend wird der Scan **10 Sekunden lang als Vorschau** eingeblendet (Dauer unter *Einstellungen → Ausgabe* änderbar, `0` schaltet sie ab). Über *Angeheftet lassen* bleibt die Vorschau offen, *Öffnen* zeigt die Originaldatei.

**Einstellungen** verwaltet Scanner, Paperless-ngx, Ausgabe, Standard-Tags, Home Assistant und das Zurücksetzen der Konfiguration.

- Unter **Scanner** entweder die eSCL-URL eintragen oder ein privates IPv4-/24-Netz durchsuchen. Die Suche prüft ausschließlich `ScannerCapabilities` und löst keinen Scan aus.
- Der Paperless-Token wird nur in `./data/config.json` gespeichert und nie wieder an den Browser ausgegeben.
- Standardformat ist **PDF**.
- *Konfiguration löschen* setzt alles zurück und startet den Assistenten erneut.

## Home Assistant

Unter *Einstellungen → Home Assistant* die Schnittstelle aktivieren; dabei wird lokal ein API-Key erzeugt. Die passende YAML-Konfiguration steht dort zum Kopieren bereit.

| Endpoint | Methode | Zweck |
| --- | --- | --- |
| `/api/ha/scan` | POST | Scan auslösen. Optionaler JSON-Body: `tags`, `source`, `resolution`, `color_mode`, `output_format`, `upload_to_paperless`, `title_prefix`. |
| `/api/ha/state` | GET | Status für einen RESTful-Sensor: `state` (`idle`/`scanning`/`error`), `progress`, `stage`, `last_file`, `last_error`. |
| `/api/ha/test` | POST | Verbindungstest. |

Authentifizierung über den Header `X-API-Key` (alternativ `Authorization: Bearer …` oder `?api_key=`). Ohne aktivierte Schnittstelle antworten die Endpunkte mit `403`.

```yaml
rest_command:
  scan_deck_scan:
    url: "http://scan-deck.local:8080/api/ha/scan"
    method: POST
    headers:
      X-API-Key: !secret scan_deck_key
      Content-Type: "application/json"
    payload: '{"tags": ["Automatisiert"]}'

automation:
  - alias: "Scan bei Bewegung am Schreibtisch"
    trigger:
      - platform: state
        entity_id: binary_sensor.schreibtisch_bewegung
        to: "on"
    action:
      - service: rest_command.scan_deck_scan
```

Als Auslöser eignet sich alles, was Home Assistant kennt: Bewegungsmelder, Zigbee-Taster, NFC-Tag, Sprachbefehl oder ein Zeitplan. Zusätzlich lässt sich unter *Webhook zurück an Home Assistant* eine URL hinterlegen — dorthin meldet Scan Deck nach jedem Scan `status`, `file`, `error` und `trigger`, sodass HA auf das Ergebnis reagieren kann.

## Paperless-ngx-Upload

Der Upload verwendet `POST /api/documents/post_document/` mit Token-Authentifizierung. Standard-Tags werden über ihre Namen in Paperless-IDs aufgelöst; optional legt die App fehlende Tags selbst an. Paperless verarbeitet das Dokument anschließend asynchron. Siehe die [Paperless-ngx API-Dokumentation](https://docs.paperless-ngx.com/api/).

## Versionierung

ScanDeck folgt [Semantic Versioning](https://semver.org/lang/de/). Die maßgebliche Versionsnummer steht in der Datei `VERSION`; von dort liest sie die Anwendung ein und gibt sie unter `/health`, in `/api/config` und unten auf der Einstellungsseite aus. Alle Änderungen stehen im [CHANGELOG](CHANGELOG.md).

Für eine neue Version: `VERSION` und den `APP_VERSION`-Build-Arg in `compose.yaml` anheben, den Changelog ergänzen, committen und taggen.

```powershell
git tag -a v1.0.1 -m "ScanDeck 1.0.1"
git push origin main --tags
```

## Lizenz

[MIT](LICENSE)

## Sicherheitsnotiz

Die Anwendung ist für ein vertrauenswürdiges Heim- oder LAN-Netz vorgesehen und hat bewusst keine eigene Benutzeranmeldung — der Home-Assistant-API-Key schützt nur die `/api/ha/*`-Endpunkte, nicht die Oberfläche. Stelle den Port `8080` nicht ungeschützt ins Internet. Paperless-Token und API-Key liegen im persistenten Volume im Klartext; schütze daher den Ordner `data` und sichere ihn nicht unbedacht in öffentliche Repositories.
