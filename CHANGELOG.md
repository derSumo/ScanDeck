# Changelog

Alle nennenswerten Änderungen an ScanDeck werden hier festgehalten.

Das Format orientiert sich an [Keep a Changelog](https://keepachangelog.com/de/1.1.0/),
die Versionierung folgt [Semantic Versioning](https://semver.org/lang/de/).

Bei `MAJOR.MINOR.PATCH` bedeutet für dieses Projekt:

- **MAJOR** — Brüche in der REST-Schnittstelle oder im Format von `config.json`, die manuelles Eingreifen erfordern.
- **MINOR** — neue Funktionen, die abwärtskompatibel bleiben.
- **PATCH** — Fehlerbehebungen und interne Verbesserungen ohne Verhaltensänderung.

## [Unreleased]

## [1.2.0] - 2026-08-10

### Hinzugefügt

- Stapel-Modus: Mehrere Einzelscans lassen sich zu einem einzigen PDF-Dokument zusammenfassen. Seiten erscheinen als Vorschauliste, einzelne Seiten können 1:1 ersetzt oder entfernt werden, und erst beim Abschluss wird das fertige Dokument abgelegt und hochgeladen. Der Stapel überlebt einen Reload und ist auf allen Geräten identisch.
- `POST /api/ha/batch` steuert den Stapel aus Home Assistant heraus (`start`, `finish`, `cancel`); der Statussensor meldet zusätzlich `batch_active` und `batch_pages`.
- Automatische Netzerkennung für die Scanner-Suche: Das Netz wird aus der Adresse des zugreifenden Geräts, der Paperless-Adresse und den üblichen Router-Standards abgeleitet und der Reihe nach durchsucht. Docker-eigene Netze werden zuletzt geprüft. Die manuelle Eingabe bleibt als Aufklappbereich erhalten.

### Geändert

- Die Scanner-Suche in Assistent und Einstellungen startet jetzt mit einem Klick, statt vorab ein Netz zu verlangen.
- README erklärt, dass die Texterkennung von Paperless-ngx kommt, und nennt die nötigen Sprachvariablen.

## [1.1.0] - 2026-08-10

### Hinzugefügt

- Versionsanzeige neben dem Logo sowie eine Versionskarte in den Einstellungen.
- Update-Hinweis: Die App fragt beim Start und danach alle sechs Stunden bei GitHub nach der neuesten Version. Liegt eine neuere vor, wird die Versionsanzeige hervorgehoben und verlinkt auf das Release. Abschaltbar über `update_check`.
- GitHub-Actions-Workflow, der das Image für `linux/amd64` und `linux/arm64` baut und in die GitHub Container Registry veröffentlicht. Er bricht ab, wenn ein Tag nicht zur `VERSION`-Datei passt.
- Fertiges Container-Image unter `ghcr.io/dersumo/scandeck`, dadurch ist kein lokaler Build mehr nötig.

### Behoben

- Der Container konnte in ein frisch gemountetes Volume nicht schreiben, weil dieses dem Host-root gehört, der Dienst aber unter UID 10001 läuft (`PermissionError: /data/config.json`). Ein Entrypoint richtet die Rechte jetzt beim Start ein und gibt die Privilegien danach wieder ab; über `PUID`/`PGID` lassen sich die IDs an den Host anpassen.
- Schreibfehler beantworten die API mit einer verständlichen Meldung statt mit einem Stacktrace, und nicht beschreibbare Verzeichnisse werden schon beim Start im Containerlog gemeldet.

### Geändert

- `compose.yaml` heißt jetzt `docker-compose.yaml` und zieht das veröffentlichte Image, statt lokal zu bauen.
- README mit direkterem Einstieg und `docker run`-Variante.

## [1.0.0] - 2026-08-10

Erste stabile Fassung.

### Hinzugefügt

- Einrichtungsassistent beim ersten Start: Scanner, Paperless-ngx, Ausgabeformat und Automatisierung in vier Schritten; jeder Schritt wird zwischengespeichert.
- Fortschrittsanzeige während des Scans mit Prozentwert, Laufzeit und Phasen (Verbindung, Erfassen, Speichern, Upload), live über Server-Sent Events.
- Vorschau nach dem Scan für standardmäßig 10 Sekunden, mit Countdown, Anheften und Öffnen der Originaldatei. PDFs werden serverseitig zur ersten Seite gerastert.
- Home-Assistant-Schnittstelle: `POST /api/ha/scan` als Auslöser, `GET /api/ha/state` als Statussensor, `POST /api/ha/test` als Verbindungstest, geschützt über einen lokal erzeugten API-Key. Optionaler Webhook zurück an Home Assistant nach jedem Scan.
- Installierbare PWA mit Manifest, Service Worker, App-Icons und Schnellaktionen für Scan und Einstellungen.
- Netzwerksuche nach eSCL-Scannern in einem privaten IPv4-/24-Netz; das Vorschlagsnetz wird aus der eigenen Adresse abgeleitet.
- Session-Tags, die nur für den nächsten Scan gelten, zusätzlich zu den Standard-Tags.
- Zurücksetzen der Konfiguration über die Oberfläche, startet den Assistenten erneut.
- Versionsanzeige in den Einstellungen sowie unter `/health` und `/api/config`.

### Geändert

- Neu gestaltete Oberfläche: Dark-Anthrazit-Theme, Dashboard mit Scan-Orb und Schnellschaltern, getrennte Einstellungsseite, mobil-optimiert mit Safe-Area-Unterstützung.
- Standardausgabeformat ist jetzt PDF statt JPEG.
- Die Auslieferung startet ohne vorkonfigurierte Endpunkte; alle Felder sind bis zum Abschluss des Assistenten leer.
- Zusätzlich unterstützte Auflösung: 150 dpi.

[Unreleased]: https://github.com/derSumo/ScanDeck/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/derSumo/ScanDeck/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/derSumo/ScanDeck/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/derSumo/ScanDeck/releases/tag/v1.0.0
