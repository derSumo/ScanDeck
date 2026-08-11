# Changelog

Alle nennenswerten Änderungen an ScanDeck werden hier festgehalten.

Das Format orientiert sich an [Keep a Changelog](https://keepachangelog.com/de/1.1.0/),
die Versionierung folgt [Semantic Versioning](https://semver.org/lang/de/).

Bei `MAJOR.MINOR.PATCH` bedeutet für dieses Projekt:

- **MAJOR** — Brüche in der REST-Schnittstelle oder im Format von `config.json`, die manuelles Eingreifen erfordern.
- **MINOR** — neue Funktionen, die abwärtskompatibel bleiben.
- **PATCH** — Fehlerbehebungen und interne Verbesserungen ohne Verhaltensänderung.

## [Unreleased]

## [1.5.0] - 2026-08-11

### Hinzugefügt

- Verlauf als eigener Reiter: jeder Scan mit Vorschaubild, Status und Datum, dazu erneut senden, öffnen und löschen. Am Reiter zeigt ein Zähler, wie viele Uploads noch offen sind.
- Rückmeldung von Paperless-ngx: ScanDeck verfolgt die Verarbeitungsaufgabe und meldet, ob das Dokument angelegt (mit Dokumentnummer), als Duplikat abgelehnt oder mit Fehler abgewiesen wurde. Bisher endete die Spur beim Hochladen.
- Warteschlange für Uploads: Scheitert die Übertragung, bleibt der Scan gespeichert und wird selbsttätig erneut versucht — nach 30 s, 2 min, 5 min, 15 min, danach stündlich.
- Aufräumen (opt-in): Lokale Kopien werden nach einer einstellbaren Wartezeit gelöscht, Standard 24 Stunden. Nur bestätigte Uploads werden entfernt; offene, abgelehnte und doppelte Dokumente bleiben liegen.
- Korrespondent und Dokumenttyp lassen sich beim Scannen wählen (opt-in). Beide Listen kommen aus der Paperless-Instanz, die Auswahl gilt nur für den nächsten Scan.
- Schnell-Tags (opt-in): die meistgenutzten Tags aus Paperless als antippbare Vorschläge; eigene Tags bleiben frei eintippbar.
- Scanner vorwärmen (opt-in, standardmäßig an): Beim Öffnen der App wird der Gerätestatus abgefragt, damit ein eingeschlafener Scanner nicht erst beim Druck auf den Knopf aufwacht.

### Geändert

- Uploads laufen grundsätzlich über die Warteschlange, auch der Abschluss eines Stapels. Der Scan gilt damit erst als erledigt, wenn Paperless ihn bestätigt hat.
- Zusatzfunktionen sind bewusst abgeschaltet ausgeliefert, damit das Dashboard schlank bleibt.

## [1.4.1] - 2026-08-10

### Behoben

- Installierte Apps (Startbildschirm auf iOS und Android) zeigten weiterhin die Oberfläche der ersten Installation: Der Service Worker trug seit 1.0.0 denselben Cache-Namen und lieferte Stylesheet und Skript grundsätzlich aus dem Cache. Damit kam kein einziges Design-Update dort an — sichtbar zuletzt daran, dass sich das Stapel-Fenster auf dem iPhone nicht schließen ließ, auf dem Desktop aber schon.
- Der Cache-Name enthält jetzt die Version, alte Caches werden beim Aktivieren gelöscht, Stylesheet und Skript werden mit Versionsnummer angefragt, und statische Dateien werden im Hintergrund erneuert (stale-while-revalidate). Eine neue Version übernimmt die App außerdem sofort, statt auf einen späteren Start zu warten.

## [1.4.0] - 2026-08-10

### Behoben

- Das Stapel-Fenster verschwand nicht, wenn der Stapel ausgeschaltet wurde, und klappte sofort wieder auf. Ursache war eine eigene `display`-Regel, die das `hidden`-Attribut überstimmte; das galt ebenso für das eingeklappte Live-Protokoll und den Seitenzähler.
- Auf dem iPhone öffnete das lange Drücken zum Verschieben zusätzlich Lupe, Textauswahl und Teilen-Menü. Auswahl und Kontextmenü sind auf den Seitenkacheln jetzt unterdrückt, und während des Ziehens wird das Mitscrollen ausdrücklich verhindert.

### Hinzugefügt

- ScanDeck misst die Dauer jedes Scans je Profil (Quelle, Auflösung, Farbmodus, Format) und speichert sie in `data/timings.json`. Ab dem zweiten Scan eines Profils läuft der Fortschrittsbalken in echter Zeit und die Anzeige nennt die verbleibenden Sekunden. Dauert ein Scan länger als gewohnt, kriecht der Balken weiter, statt stehen zu bleiben.

## [1.3.0] - 2026-08-10

### Hinzugefügt

- Seiten im Stapel lassen sich per Ziehen umsortieren — auf dem Handy nach kurzem Halten, am Rechner direkt. Die Kacheln weichen dabei animiert aus, ein normaler Wisch scrollt weiterhin.
- Einzelne Seiten können um 90° gedreht werden. Die Drehung ist sofort in der Vorschau sichtbar und landet genauso im fertigen PDF.
- Fragezeichen neben „Seiten im Stapel“ blendet eine kurze Erklärung der vier Seitenwerkzeuge ein.

### Geändert

- Das Stapel-Fenster klappt animiert auf und zu, statt hart zu erscheinen.
- Während ein Stapel gesammelt wird, zeigt die Fortschrittsanzeige nur noch drei Schritte; der Upload-Schritt erscheint erst beim Abschließen.
- Die Verbindung zum Scanner bleibt zwischen den drei Anfragen eines Scans offen (HTTP Keep-alive), was den TLS-Handshake zweimal pro Scan einspart. Die Dauer eines Scans steht jetzt im Live-Protokoll.

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

[Unreleased]: https://github.com/derSumo/ScanDeck/compare/v1.5.0...HEAD
[1.5.0]: https://github.com/derSumo/ScanDeck/compare/v1.4.1...v1.5.0
[1.4.1]: https://github.com/derSumo/ScanDeck/compare/v1.4.0...v1.4.1
[1.4.0]: https://github.com/derSumo/ScanDeck/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/derSumo/ScanDeck/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/derSumo/ScanDeck/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/derSumo/ScanDeck/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/derSumo/ScanDeck/releases/tag/v1.0.0
