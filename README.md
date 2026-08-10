# ScanDeck

**Dokument auf den Scanner legen, Handy zücken, Knopf drücken — fertig, es liegt in Paperless-ngx.**

Kein PC hochfahren, keine Scan-Software, kein USB-Kabel, kein Umweg über den Download-Ordner. ScanDeck spricht direkt mit deinem Netzwerkscanner (HP, Brother, Canon, Epson — alles, was eSCL/AirScan kann) und schiebt das Ergebnis als PDF sofort in Paperless-ngx. Bedient wird es über eine Webseite, die auf dem Handy aussieht und sich anfühlt wie eine App — du kannst sie dir auf den Homescreen legen.

Der Ablauf, den du danach hast: Post aufmachen, Blatt auf die Glasplatte, am Handy auf *Scan starten* tippen, kurz die Vorschau sehen — und das Dokument ist getaggt in Paperless. Das sind ein paar Sekunden statt einer kleinen Bastelrunde pro Brief.

Und wenn es noch weniger sein soll: Über die Home-Assistant-Schnittstelle löst ein Taster, ein NFC-Tag oder ein Bewegungsmelder den Scan aus. Dann drückst du nicht mal mehr aufs Handy.

| Dashboard | Scanvorgang |
| --- | --- |
| ![Dashboard](assets/mainpage.png) | ![Scanfortschritt](assets/scan.png) |

## Loslegen

Das fertige Image liegt in der GitHub Container Registry, gebaut für `amd64` und `arm64` (läuft also auch auf dem Raspberry Pi). Nichts kompilieren, einfach ziehen.

**Mit Docker Compose** — [`docker-compose.yaml`](docker-compose.yaml) herunterladen und starten:

```bash
docker compose up -d
```

**Oder mit einem einzelnen Befehl:**

```bash
docker run -d \
  --name scandeck \
  -p 8080:8080 \
  -v ./data:/data \
  -v ./scans:/scans \
  --restart unless-stopped \
  ghcr.io/dersumo/scandeck:latest
```

Dann `http://localhost:8080` im Browser öffnen (oder vom Handy aus die IP des Servers, z. B. `http://192.168.1.10:8080`).

Beim ersten Start ist **nichts vorkonfiguriert** — es begrüßt dich ein Einrichtungsassistent, der den Scanner im Netzwerk sucht, nach Paperless-ngx fragt und Format sowie Tags festlegt. Danach ist Ruhe, du siehst nur noch den Scan-Knopf.

Einstellungen landen in `./data/config.json`, die Scans in `./scans`. Beides sind Volumes und überleben ein Update.

Aktualisieren und stoppen:

```bash
docker compose pull && docker compose up -d
docker compose down
```

Wer das Image lieber selbst baut, klont das Repository und nutzt das mitgelieferte [Dockerfile](Dockerfile):

```bash
docker build -t scandeck .
```

### Dateirechte

Darum musst du dich normalerweise nicht kümmern: Der Container startet als root, setzt die Rechte der gemounteten Ordner `./data` und `./scans` einmalig zurecht und gibt die Privilegien dann ab — der Dienst selbst läuft unprivilegiert als UID 10001.

Sollen die Dateien einem bestimmten Host-Benutzer gehören, dessen IDs setzen (auf dem Host mit `id -u` und `id -g` ablesen):

```yaml
environment:
  PUID: 1000
  PGID: 1000
```

Wenn der Container per `user:` in der Compose-Datei ohne root-Rechte gestartet wird, kann er die Ordner nicht selbst korrigieren. Dann muss das einmal auf dem Host passieren:

```bash
sudo chown -R 1000:1000 ./data ./scans
```

Fehlen die Rechte, sagt ScanDeck das beim Start im Containerlog und beim Speichern als Fehlermeldung in der Oberfläche.

## Als App aufs Handy legen (PWA)

Die Oberfläche ist installierbar: In Chrome/Android über *Zum Startbildschirm hinzufügen*, in Safari/iOS über *Teilen → Zum Home-Bildschirm*. Sie läuft dann im Vollbild ohne Browserleiste, respektiert die Safe-Area und funktioniert offline so weit, dass die Oberfläche lädt (Scans brauchen natürlich das Netzwerk). Schnellaktionen im App-Icon: *Sofort scannen* und *Einstellungen*.

Damit iOS die App installieren lässt, muss die Seite über HTTPS oder `localhost` erreichbar sein — im LAN also am besten hinter einem Reverse Proxy mit Zertifikat.

## Bedienung

**Dashboard** ist auf den täglichen Ablauf reduziert: großer Scan-Button mit Fortschrittsring, vier antippbare Schnellschalter (Quelle, Format, DPI, Farbe — jeder Tipp schaltet weiter und speichert sofort), Session-Tags nur für den nächsten Scan, Statuskacheln und ein einklappbares Live-Protokoll.

Während des Scans erscheint eine Fortschrittsanzeige mit Prozentwert, Laufzeit und Phasen (Verbindung → Erfassen → Speichern → Upload). Anschließend wird der Scan **10 Sekunden lang als Vorschau** eingeblendet (Dauer unter *Einstellungen → Ausgabe* änderbar, `0` schaltet sie ab). Über *Angeheftet lassen* bleibt die Vorschau offen, *Öffnen* zeigt die Originaldatei.

### Mehrere Seiten in eine PDF

Für alles, was länger als ein Blatt ist: Auf dem Dashboard den Schalter **Stapel** umlegen. Ab dann landet jeder Scan als Seite im Stapel statt sofort in Paperless. Du scannst also Blatt für Blatt, siehst die Seiten als Vorschaubilder untereinander und drückst am Ende **Als PDF ablegen** — daraus wird ein einziges Dokument, das als Ganzes hochgeladen wird.

Was du mit einer einzelnen Seite machen kannst — das Fragezeichen neben der Überschrift erklärt es auch in der App:

- **Ziehen** — Seite antippen, kurz halten und an die richtige Stelle schieben. Falls Seite 1 und 2 vertauscht sind, muss nichts neu gescannt werden. Funktioniert auf dem Handy wie am Rechner; ein normaler Wisch scrollt weiterhin die Seite.
- **↻ Drehen** — dreht die Seite um 90° im Uhrzeigersinn, genau so wie sie später im PDF liegt. Mehrfach drücken dreht weiter.
- **⟳ Neu scannen** — markiert die Seite; der nächste Scan tauscht genau diese Seite 1:1 aus, die Reihenfolge bleibt. Nochmal drücken bricht ab.
- **× Entfernen** — wirft die Seite aus dem Stapel.

Während ein Stapel gesammelt wird, zeigt die Fortschrittsanzeige nur drei Schritte — es wird ja nichts hochgeladen. Der Upload-Schritt taucht erst beim Abschließen wieder auf.

Während eines Stapels erscheint bewusst keine Vollbild-Vorschau nach jeder Seite; das Vorschaubild in der Liste reicht und hält den Ablauf schnell. Erst das fertige Dokument wird wieder groß angezeigt. Der Stapel überlebt einen Seiten-Reload und ist auf allen Geräten gleich — du kannst also am Handy scannen und am Rechner sortieren.

Das Format des Stapels ist immer PDF, unabhängig von der Formateinstellung. Einzelne Seiten dürfen gemischt sein (PDF vom Scanner oder JPEG), sie werden beim Zusammenführen vereinheitlicht. Auch der automatische Einzug (ADF) funktioniert im Stapel: Er liefert pro Scan mehrere Seiten, die alle angehängt werden.

**Einstellungen** verwaltet Scanner, Paperless-ngx, Ausgabe, Standard-Tags, Home Assistant und das Zurücksetzen der Konfiguration.

- Unter **Scanner** genügt ein Klick auf *Netzwerk automatisch durchsuchen*. ScanDeck rät das Netz nicht, sondern leitet es aus der Adresse des Geräts ab, mit dem du gerade die Oberfläche geöffnet hast — das steht im selben Netz wie der Scanner. Zusätzlich werden die Netze deiner Paperless-Adresse und die üblichen Router-Standards (`192.168.0.x`, `192.168.1.x`, `192.168.178.x`, `10.0.0.x`) geprüft, bis etwas gefunden wird. Docker-eigene Netze werden dabei ans Ende gestellt, weil dort nie ein Scanner steht. Manuell geht weiterhin über *Netzwerk manuell angeben*. Die Suche prüft ausschließlich `ScannerCapabilities` und löst keinen Scan aus.
- Der Paperless-Token wird nur in `./data/config.json` gespeichert und nie wieder an den Browser ausgegeben.
- Standardformat ist **PDF**.
- *Konfiguration löschen* setzt alles zurück und startet den Assistenten erneut.

## Home Assistant

Unter *Einstellungen → Home Assistant* die Schnittstelle aktivieren; dabei wird lokal ein API-Key erzeugt. Die passende YAML-Konfiguration steht dort zum Kopieren bereit.

| Endpoint | Methode | Zweck |
| --- | --- | --- |
| `/api/ha/scan` | POST | Scan auslösen. Optionaler JSON-Body: `tags`, `source`, `resolution`, `color_mode`, `output_format`, `upload_to_paperless`, `title_prefix`. |
| `/api/ha/state` | GET | Status für einen RESTful-Sensor: `state` (`idle`/`scanning`/`error`), `progress`, `stage`, `last_file`, `last_error`. |
| `/api/ha/batch` | POST | Stapel steuern: `{"action": "start"}`, `"finish"` oder `"cancel"`. Bei laufendem Stapel wird jeder ausgelöste Scan zu einer weiteren Seite. |
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

Damit lässt sich auch ein Stapel ohne Handy bedienen: ein Taster startet den Stapel, jeder weitere Druck scannt eine Seite, langes Drücken schließt ab.

Als Auslöser eignet sich alles, was Home Assistant kennt: Bewegungsmelder, Zigbee-Taster, NFC-Tag, Sprachbefehl oder ein Zeitplan. Zusätzlich lässt sich unter *Webhook zurück an Home Assistant* eine URL hinterlegen — dorthin meldet Scan Deck nach jedem Scan `status`, `file`, `error` und `trigger`, sodass HA auf das Ergebnis reagieren kann.

## Paperless-ngx-Upload

### Geschwindigkeit

Ein Scan besteht aus drei Anfragen an den Scanner: Status abfragen, Auftrag anlegen, Dokument abholen. ScanDeck hält die HTTPS-Verbindung dazwischen offen, statt sie dreimal neu aufzubauen — der TLS-Handshake fällt damit nur einmal an, und Drucker-Firmware ist beim Verschlüsseln meist ziemlich langsam. Wie lange ein Scan gebraucht hat, steht im Live-Protokoll hinter dem Dateinamen.

Der Rest der Wartezeit ist der Scanner selbst: Lampe aufwärmen, Schlitten fahren, Bild übertragen. Zwei Stellschrauben helfen spürbar:

- **Auflösung** — 600 dpi dauert grob viermal so lange wie 300 dpi und bringt bei normalem Papier nichts. Für Briefe reichen 200–300 dpi.
- **Farbmodus** — Graustufen überträgt ein Drittel der Daten von Farbe.

Manche Geräte bieten eSCL zusätzlich unverschlüsselt auf Port 80 an (`http://scanner-ip:80` statt `https://scanner-ip:443`). Das spart den Handshake ganz. Im eigenen LAN ist das vertretbar; über Netzgrenzen hinweg bleib bei HTTPS.

### Durchsuchbare PDFs (OCR)

ScanDeck macht **kein** OCR. Der Scanner liefert ein reines Bild-PDF, und genau das wird abgelegt und hochgeladen. Die Texterkennung übernimmt Paperless-ngx beim Import mit OCRmyPDF/Tesseract — dort entsteht das durchsuchbare Dokument. Die Datei in `./scans` bleibt dagegen immer ein Bild-PDF.

Wichtig ist dabei die Sprache: Paperless-ngx erkennt standardmäßig **Englisch**. Für deutsche Dokumente gehört in dessen Compose-Datei:

```yaml
environment:
  PAPERLESS_OCR_LANGUAGE: deu
  PAPERLESS_OCR_LANGUAGES: deu eng
```

`PAPERLESS_OCR_LANGUAGE` ist die Sprache, mit der erkannt wird; `PAPERLESS_OCR_LANGUAGES` bestimmt, welche Sprachpakete überhaupt installiert werden. Ohne das zweite fehlt Tesseract das deutsche Modell. Ab Paperless-ngx 2.x lässt sich die Sprache alternativ in dessen Oberfläche unter *Einstellungen → Konfiguration* setzen; ist sie dort leer, gilt die Umgebungsvariable.

Kontrollieren lässt sich das an einem importierten Dokument: Enthält der Textinhalt in Paperless deutsche Umlaute statt Zeichensalat, passt die Sprache.

### Upload

Der Upload verwendet `POST /api/documents/post_document/` mit Token-Authentifizierung. Standard-Tags werden über ihre Namen in Paperless-IDs aufgelöst; optional legt die App fehlende Tags selbst an. Paperless verarbeitet das Dokument anschließend asynchron. Siehe die [Paperless-ngx API-Dokumentation](https://docs.paperless-ngx.com/api/).

## Versionierung

ScanDeck folgt [Semantic Versioning](https://semver.org/lang/de/). Die maßgebliche Versionsnummer steht in der Datei `VERSION`; von dort liest sie die Anwendung ein und zeigt sie dezent neben dem Logo an. Alle Änderungen stehen im [CHANGELOG](CHANGELOG.md).

Die App sieht einmal beim Start und danach alle sechs Stunden bei GitHub nach, ob es ein neueres Release gibt. Falls ja, färbt sich die Versionsanzeige neben dem Logo amber und bekommt einen kleinen Pfeil — ein Tipp darauf öffnet die Release-Seite. Unter *Einstellungen → Version* lässt sich sofort prüfen oder die Abfrage ganz abschalten; sie geht ausschließlich an `api.github.com` und sendet nichts über deine Installation.

Für eine neue Version: `VERSION` anheben, den Changelog ergänzen, committen und taggen. Der Workflow [`docker-publish.yml`](.github/workflows/docker-publish.yml) baut daraufhin das Image für amd64 und arm64 und veröffentlicht es unter der neuen Versionsnummer — er bricht ab, wenn Tag und `VERSION` nicht zusammenpassen.

```powershell
git tag -a v1.0.1 -m "ScanDeck 1.0.1"
git push origin main --tags
```

## Lizenz

[MIT](LICENSE)

## Sicherheitsnotiz

Die Anwendung ist für ein vertrauenswürdiges Heim- oder LAN-Netz vorgesehen und hat bewusst keine eigene Benutzeranmeldung — der Home-Assistant-API-Key schützt nur die `/api/ha/*`-Endpunkte, nicht die Oberfläche. Stelle den Port `8080` nicht ungeschützt ins Internet. Paperless-Token und API-Key liegen im persistenten Volume im Klartext; schütze daher den Ordner `data` und sichere ihn nicht unbedacht in öffentliche Repositories.
