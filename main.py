import time
import urllib3
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

import requests


# ============================================================
# KONFIGURATION
# ============================================================

PRINTER_URL = "https://10.0.0.31:443"

VERIFY_SSL = False

OUTPUT_FILE = "scan.jpg"

RESOLUTION = 300
COLOR_MODE = "RGB24"
DOCUMENT_FORMAT = "image/jpeg"
INPUT_SOURCE = "Platen"

# HP eSCL scanners commonly require the compatible client version 2.0
# for ScanJobs, even if ScannerCapabilities reports a newer device version.
REQUEST_ESCL_VERSION = "2.0"

# A4 in 1/300 Zoll
SCAN_WIDTH = 2480
SCAN_HEIGHT = 3508


# ============================================================
# NAMESPACES
# ============================================================

PWG_NS = "http://www.pwg.org/schemas/2010/12/sm"
SCAN_NS = "http://schemas.hp.com/imaging/escl/2011/05/03"


# ============================================================
# SESSION
# ============================================================

def create_session():
    session = requests.Session()
    session.verify = VERIFY_SSL

    if not VERIFY_SSL:
        urllib3.disable_warnings(
            urllib3.exceptions.InsecureRequestWarning
        )

    return session


# ============================================================
# CAPABILITIES
# ============================================================

def get_capabilities(session):
    url = f"{PRINTER_URL}/eSCL/ScannerCapabilities"

    print("[1/5] ScannerCapabilities abrufen...")

    response = session.get(
        url,
        timeout=15
    )

    print(f"HTTP Status: {response.status_code}")

    response.raise_for_status()

    print("Scanner erreichbar.")
    print()

    return response.text


# ============================================================
# PWG VERSION AUSLESEN
# ============================================================

def get_pwg_version(capabilities_xml):
    root = ET.fromstring(capabilities_xml)

    version_element = root.find(
        f".//{{{PWG_NS}}}Version"
    )

    if version_element is None:
        raise RuntimeError(
            "pwg:Version wurde in ScannerCapabilities nicht gefunden."
        )

    version = version_element.text.strip()

    return version


# ============================================================
# STATUS
# ============================================================

def get_scanner_status(session):
    url = f"{PRINTER_URL}/eSCL/ScannerStatus"

    print("[2/5] ScannerStatus abrufen...")

    response = session.get(
        url,
        timeout=15
    )

    print(f"HTTP Status: {response.status_code}")

    if response.status_code != 200:
        print("ScannerStatus konnte nicht gelesen werden.")
        print()
        return

    text = response.text

    if "<pwg:State>Idle</pwg:State>" in text:
        print("Scanner ist bereit: Idle")

    elif "<pwg:State>Processing</pwg:State>" in text:
        print("Scanner arbeitet gerade: Processing")

    elif "<pwg:State>Stopped</pwg:State>" in text:
        print("Scanner meldet: Stopped")

    else:
        print("ScannerStatus empfangen.")
        print()
        print(text[:2000])

    print()


# ============================================================
# SCAN SETTINGS ERSTELLEN
# ============================================================

def build_scan_settings():
    # This HP firmware rejects the otherwise equivalent, heavily indented
    # form. Keep this proven interoperable eSCL layout compact.
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScanSettings xmlns:pwg="{PWG_NS}" xmlns:scan="{SCAN_NS}">
  <pwg:Version>{REQUEST_ESCL_VERSION}</pwg:Version>
  <pwg:ScanRegions>
    <pwg:ScanRegion>
      <pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>
      <pwg:XOffset>0</pwg:XOffset>
      <pwg:YOffset>0</pwg:YOffset>
      <pwg:Width>{SCAN_WIDTH}</pwg:Width>
      <pwg:Height>{SCAN_HEIGHT}</pwg:Height>
    </pwg:ScanRegion>
  </pwg:ScanRegions>
  <pwg:InputSource>{INPUT_SOURCE}</pwg:InputSource>
  <scan:ColorMode>{COLOR_MODE}</scan:ColorMode>
  <pwg:DocumentFormat>{DOCUMENT_FORMAT}</pwg:DocumentFormat>
  <scan:DocumentFormatExt>{DOCUMENT_FORMAT}</scan:DocumentFormatExt>
  <scan:XResolution>{RESOLUTION}</scan:XResolution>
  <scan:YResolution>{RESOLUTION}</scan:YResolution>
</scan:ScanSettings>"""


# ============================================================
# SCAN JOB ERSTELLEN
# ============================================================

def create_scan_job(session, scan_settings):
    url = f"{PRINTER_URL}/eSCL/ScanJobs"

    print("[4/5] ScanJob erstellen...")
    print()
    print("Gesendete ScanSettings:")
    print("-" * 60)
    print(scan_settings)
    print("-" * 60)
    print()

    headers = {
        "Content-Type": "text/xml",
        "Accept": "*/*",
        "Connection": "keep-alive",
    }

    response = session.post(
        url,
        data=scan_settings.encode("utf-8"),
        headers=headers,
        timeout=30
    )

    print(f"HTTP Status: {response.status_code}")
    print()

    print("Antwort-Header:")

    for key, value in response.headers.items():
        print(f"  {key}: {value}")

    print()

    if response.status_code not in (200, 201):
        print("ScanJob wurde abgelehnt.")
        print()

        if response.status_code == 409:
            print("HTTP 409 Conflict")
            print(
                "Der Drucker akzeptiert mindestens eine "
                "Scan-Einstellung nicht."
            )

        if response.text:
            print()
            print("Antwort:")
            print(response.text[:5000])
        else:
            print("Antwort-Body ist leer.")

        return None

    location = response.headers.get("Location")

    if not location:
        print("FEHLER:")
        print("Kein Location-Header vom Drucker erhalten.")
        return None

    job_url = urljoin(
        PRINTER_URL + "/",
        location
    )

    print("ScanJob erfolgreich erstellt.")
    print(f"Location: {location}")
    print(f"Job URL:  {job_url}")
    print()

    return job_url


# ============================================================
# NEXT DOCUMENT
# ============================================================

def download_document(session, job_url):
    next_document_url = (
        job_url.rstrip("/")
        + "/NextDocument"
    )

    print("[5/5] Dokument abrufen...")
    print(f"URL: {next_document_url}")
    print()

    try:
        response = session.get(
            next_document_url,
            headers={
                "Accept": (
                    "image/jpeg, "
                    "application/pdf, "
                    "application/octet-stream"
                )
            },
            timeout=180
        )

    except requests.Timeout:
        print("Timeout beim Warten auf den Scan.")
        return False

    except requests.RequestException as error:
        print("Fehler beim Abrufen:")
        print(error)
        return False

    print(f"HTTP Status: {response.status_code}")

    if response.status_code != 200:
        print()
        print("Dokument konnte nicht geladen werden.")

        if response.text:
            print(response.text[:5000])

        return False

    if not response.content:
        print("Drucker hat eine leere Antwort geliefert.")
        return False

    content_type = response.headers.get(
        "Content-Type",
        ""
    )

    print(f"Content-Type: {content_type}")
    print(f"Größe: {len(response.content):,} Bytes")

    output_file = OUTPUT_FILE

    if "pdf" in content_type.lower():
        output_file = "scan.pdf"

    elif "jpeg" in content_type.lower():
        output_file = "scan.jpg"

    with open(output_file, "wb") as file:
        file.write(response.content)

    print()
    print("=" * 60)
    print("SCAN ERFOLGREICH")
    print("=" * 60)
    print(f"Datei: {output_file}")
    print()

    return True


# ============================================================
# JOB AUFRÄUMEN
# ============================================================

def delete_scan_job(session, job_url):
    try:
        response = session.delete(
            job_url,
            timeout=10
        )

        print(
            f"ScanJob löschen: HTTP "
            f"{response.status_code}"
        )

    except requests.RequestException:
        pass


# ============================================================
# MAIN
# ============================================================

def main():
    print()
    print("=" * 60)
    print("HP DeskJet 4100 eSCL Test")
    print("=" * 60)
    print()
    print(f"Drucker: {PRINTER_URL}")
    print()

    session = create_session()

    # --------------------------------------------------------
    # 1. ScannerCapabilities
    # --------------------------------------------------------

    try:
        capabilities_xml = get_capabilities(
            session
        )

    except requests.RequestException as error:
        print("Scanner nicht erreichbar:")
        print(error)
        return

    except Exception as error:
        print("Fehler:")
        print(error)
        return

    # --------------------------------------------------------
    # 2. Geraeteversion nur zur Diagnose auslesen
    # --------------------------------------------------------

    try:
        device_pwg_version = get_pwg_version(
            capabilities_xml
        )

    except Exception as error:
        print("Fehler beim Auslesen der PWG-Version:")
        print(error)
        return

    print("[3/5] eSCL/PWG-Version des Druckers:")
    print()
    print(f"    {device_pwg_version}")
    print()
    print("Fuer ScanJobs verwendete Client-Version:")
    print()
    print(f"    {REQUEST_ESCL_VERSION}")
    print()

    # --------------------------------------------------------
    # 3. Status
    # --------------------------------------------------------

    try:
        get_scanner_status(session)

    except requests.RequestException as error:
        print(
            "ScannerStatus konnte nicht "
            "abgerufen werden:"
        )
        print(error)
        print()

    # --------------------------------------------------------
    # 4. ScanSettings
    # --------------------------------------------------------

    scan_settings = build_scan_settings()

    # --------------------------------------------------------
    # 5. Job erstellen
    # --------------------------------------------------------

    try:
        job_url = create_scan_job(
            session,
            scan_settings
        )

    except requests.RequestException as error:
        print("Fehler beim Erstellen des ScanJobs:")
        print(error)
        return

    if not job_url:
        return

    # --------------------------------------------------------
    # 6. Dokument holen
    # --------------------------------------------------------

    success = download_document(
        session,
        job_url
    )

    # --------------------------------------------------------
    # 7. Aufräumen
    # --------------------------------------------------------

    if success:
        delete_scan_job(
            session,
            job_url
        )


if __name__ == "__main__":
    main()
