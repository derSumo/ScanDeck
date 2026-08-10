# Changelog

Alle nennenswerten Änderungen an ScanDeck werden hier festgehalten.

Das Format orientiert sich an [Keep a Changelog](https://keepachangelog.com/de/1.1.0/),
die Versionierung folgt [Semantic Versioning](https://semver.org/lang/de/).

Bei `MAJOR.MINOR.PATCH` bedeutet für dieses Projekt:

- **MAJOR** — Brüche in der REST-Schnittstelle oder im Format von `config.json`, die manuelles Eingreifen erfordern.
- **MINOR** — neue Funktionen, die abwärtskompatibel bleiben.
- **PATCH** — Fehlerbehebungen und interne Verbesserungen ohne Verhaltensänderung.

## [Unreleased]

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

[Unreleased]: https://github.com/derSumo/ScanDeck/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/derSumo/ScanDeck/releases/tag/v1.0.0
