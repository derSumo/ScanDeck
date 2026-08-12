"""ScanDeck: the HTTP surface and the workers that drive a scan.

Everything that can stand on its own lives in the scandeck package; this module
wires those parts to Flask, owns the mutable run state and keeps the background
loop for uploads, confirmations and cleanup.
"""

from __future__ import annotations

import secrets
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any, Callable

import requests
from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    stream_with_context,
)

from scandeck import auth
from scandeck.batch import BatchCollector
from scandeck.config import (
    APP_DATA_DIR,
    BATCH_DIR,
    CONFIG_PATH,
    JOBS_PATH,
    PAPER_SIZES,
    TIMINGS_PATH,
    ConfigStore,
    validate_config,
)
from scandeck.documents import (
    merge_files,
    normalise_rotation,
    page_count,
    preview_cache,
    render_preview,
)
from scandeck.escl import ScannerClient
from scandeck.events import LogHub, TooManySubscribers
from scandeck.jobs import JobStore, TimingStore
from scandeck.network import (
    autodiscover_scanners,
    candidate_subnets,
    discover_escl_scanners,
    guess_local_subnet,
)
from scandeck.paperless import PaperlessClient
from scandeck.updates import check_for_update
from scandeck.version import APP_VERSION, RELEASES_URL

# Settings a single scan may override without being stored.
SCAN_OVERRIDES = (
    "source", "resolution", "color_mode", "output_format", "paper_size", "duplex",
    "upload_to_paperless", "title_prefix", "correspondent", "document_type",
)

app = Flask(__name__)
logs = LogHub()
store = ConfigStore(CONFIG_PATH)
timings = TimingStore(TIMINGS_PATH)
jobs = JobStore(JOBS_PATH)
batch = BatchCollector(BATCH_DIR)

scan_lock = threading.Lock()
scan_state: dict[str, Any] = {
    "running": False,
    "stage": "idle",
    "progress": 0,
    "last_file": None,
    "last_name": None,
    "last_kind": None,
    "last_finished": None,
    "last_error": None,
    "trigger": None,
}


def _mirror_progress(stage: str, percent: int) -> None:
    """Keep the polled state in step with the streamed one."""
    scan_state["stage"] = stage
    scan_state["progress"] = percent


logs.on_progress = _mirror_progress


# --------------------------------------------------------------------------- #
# Optional access protection
# --------------------------------------------------------------------------- #

login_throttle = auth.LoginThrottle()


def session_secret() -> str:
    """A signing key that survives restarts, so nobody is logged out by a redeploy."""
    secret = store.get().get("session_secret")
    if secret:
        return secret
    secret = secrets.token_urlsafe(48)
    try:
        store.patch(session_secret=secret)
    except (OSError, ValueError) as error:
        # A read-only volume or a stored setting that no longer validates must
        # not stop the service from coming up at all. Sessions then last until
        # the next restart, and the real problem is reported where it belongs:
        # the moment someone saves the settings.
        print(f"ScanDeck: Sitzungsschluessel nicht gespeichert ({error}).", file=sys.stderr, flush=True)
    return secret


app.secret_key = session_secret()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)


def is_authenticated() -> bool:
    config = store.get()
    if not config.get("auth_enabled"):
        return True
    # The stamp ties a session to the current password: changing it logs
    # everyone else out, which is the point of changing it.
    return session.get("auth") == config.get("auth_password_hash")


def sign_in() -> None:
    session.permanent = True
    session["auth"] = store.get().get("auth_password_hash")


# Routes that bring their own key. Filled by @require_api_key, so a new Home
# Assistant endpoint is covered without anyone having to remember a list.
API_KEY_ROUTES: set[str] = set()


@app.before_request
def guard() -> Any:
    if auth.is_open(request.endpoint) or request.endpoint in API_KEY_ROUTES:
        return None
    if is_authenticated():
        return None
    return jsonify({"error": "Anmeldung erforderlich.", "auth_required": True}), 401


def check_writable(directory: Path, label: str) -> None:
    """Report an unwritable volume at boot instead of at the first save."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".scandeck-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as error:
        message = (
            f"{label} ({directory}) ist nicht beschreibbar: {error.strerror or error}. "
            "Bitte die Rechte des gemounteten Ordners pruefen oder PUID/PGID setzen."
        )
        logs.publish(message, "error")
        print(f"ScanDeck: {message}", file=sys.stderr, flush=True)


check_writable(APP_DATA_DIR, "Konfigurationsverzeichnis")
check_writable(Path(store.get()["output_dir"]), "Scan-Ablage")


# --------------------------------------------------------------------------- #
# Upload queue, Paperless confirmation and cleanup
# --------------------------------------------------------------------------- #

def queue_upload(file_path: Path, config: dict[str, Any], pages: int, tags: list[str]) -> dict[str, Any]:
    """Record the scan, then start the upload beside the scan.

    The attempt runs in its own thread so a slow or unreachable Paperless does
    not keep the scanner blocked for a minute and a half. The job is recorded
    first, so even if this process dies right here the queue worker picks the
    upload up again after the restart.
    """
    status = "pending" if config["upload_to_paperless"] else "local"
    job = jobs.add(file_path, status, pages=pages, tags=tags)
    if status == "pending":
        threading.Thread(target=attempt_upload, args=(job["id"], config), daemon=True).start()
    return job


def attempt_upload(job_id: str, config: dict[str, Any] | None = None) -> bool:
    """One upload attempt. Failures stay in the queue instead of vanishing."""
    job = jobs.claim(job_id)
    if not job:
        return False  # already gone, or another thread is uploading it
    path = Path(job["path"])
    if not path.exists():
        jobs.update(job_id, status="failed", error="Datei ist nicht mehr vorhanden.")
        return False

    config = config or store.get()
    try:
        task_id = PaperlessClient(config, logs).upload(path)
        jobs.update(job_id, status="processing", task_id=task_id or None, error=None, next_attempt=time.time() + 5)
        if not task_id:
            # Without a task id there is nothing to follow up on.
            jobs.update(job_id, status="success", confirmed_at=datetime.now().isoformat(timespec="seconds"))
        return True
    except (requests.RequestException, RuntimeError) as error:
        jobs.schedule_retry(job_id, str(error))
        logs.publish(f"Upload von {job['name']} fehlgeschlagen, wird erneut versucht: {error}", "warning")
        return False


def follow_up_task(job: dict[str, Any], config: dict[str, Any]) -> None:
    """Ask Paperless whether the document really made it."""
    try:
        state = PaperlessClient(config, logs).task_state(job["task_id"])
    except (requests.RequestException, RuntimeError) as error:
        jobs.update(job["id"], next_attempt=time.time() + 60, error=str(error))
        return

    status = state["status"]
    if status in ("pending", "started", "received", "retry", "unknown"):
        jobs.update(job["id"], next_attempt=time.time() + 10)
        return

    if status == "success":
        jobs.update(
            job["id"],
            status="success",
            document_id=state["document_id"],
            error=None,
            confirmed_at=datetime.now().isoformat(timespec="seconds"),
        )
        document = f" als Dokument #{state['document_id']}" if state["document_id"] else ""
        logs.publish(f"Paperless hat {job['name']}{document} angelegt.", "success")
        return

    duplicate = state["duplicate"]
    jobs.update(
        job["id"],
        status="duplicate" if duplicate else "failed",
        error=state["message"] or "Paperless hat das Dokument abgelehnt.",
        confirmed_at=datetime.now().isoformat(timespec="seconds"),
    )
    logs.publish(
        f"Paperless hat {job['name']} " + ("als Duplikat abgelehnt." if duplicate else f"abgelehnt: {state['message']}"),
        "warning" if duplicate else "error",
    )


def cleanup_uploaded(config: dict[str, Any]) -> None:
    """Delete local copies of documents Paperless confirmed, after the grace period."""
    if not config.get("cleanup_enabled"):
        return
    limit = datetime.now().timestamp() - config["cleanup_hours"] * 3600
    for job in jobs.list(limit=JobStore.MAX_JOBS):
        if job["status"] != "success" or job.get("file_deleted"):
            continue
        stamp = job.get("confirmed_at") or job.get("created")
        try:
            confirmed = datetime.fromisoformat(stamp).timestamp()
        except (TypeError, ValueError):
            continue
        if confirmed > limit:
            continue
        path = Path(job["path"])
        try:
            existed = path.exists()
            path.unlink(missing_ok=True)
        except OSError as error:
            logs.publish(f"{path.name} konnte nicht gelöscht werden: {error}", "warning")
            continue
        jobs.update(job["id"], file_deleted=True)
        if existed:
            logs.publish(f"Lokale Kopie aufgeräumt: {job['name']}")


def queue_worker() -> None:
    """Retries, confirmations and cleanup — one calm loop, every 15 seconds."""
    while True:
        try:
            config = store.get()
            if config["upload_to_paperless"]:
                for job in jobs.due("pending"):
                    attempt_upload(job["id"], config)
                for job in jobs.due("processing"):
                    if job.get("task_id"):
                        follow_up_task(job, config)
            cleanup_uploaded(config)
        except Exception as error:  # the worker must never die
            print(f"ScanDeck: Warteschlange: {error}", file=sys.stderr, flush=True)
        time.sleep(15)


# --------------------------------------------------------------------------- #
# Scan workflow
# --------------------------------------------------------------------------- #

def finish_batch(config: dict[str, Any], session_tags: list[str]) -> Path:
    """Merge, store and (optionally) upload the collected pages as one document."""
    pages = batch.pages()
    if not pages:
        raise RuntimeError("Der Stapel enthält keine Seiten.")

    logs.progress("merge", 30)
    logs.publish(f"Füge {len(pages)} Seite(n) zu einem PDF zusammen …")
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    target = Path(config["output_dir"]) / f"scan_{timestamp}_{len(pages)}-seiten.pdf"
    count = batch.merge_into(target)
    logs.publish(f"Dokument erstellt: {target.name} ({count} Seiten)", "success")

    remember_result(target, "pdf")

    upload_config = {**config}
    if session_tags:
        upload_config["default_tags"] = list(dict.fromkeys(config["default_tags"] + session_tags))
    if config["upload_to_paperless"]:
        logs.progress("upload", 80)
    else:
        logs.publish("Upload nach Paperless-ngx ist deaktiviert.", "warning")
    queue_upload(target, upload_config, count, upload_config["default_tags"])

    batch.clear()
    return target


def remember_result(target: Path, kind: str) -> None:
    preview_cache.clear()
    scan_state.update({
        "last_file": str(target),
        "last_name": target.name,
        "last_kind": kind,
        "last_finished": datetime.now().isoformat(timespec="seconds"),
    })


def start_scan_job(session_tags: list[str], trigger: str = "ui", overrides: dict[str, Any] | None = None) -> bool:
    """Kick off a scan unless one is already running. Returns False if busy."""
    if not scan_lock.acquire(blocking=False):
        return False
    scan_state.update({
        "running": True,
        "last_error": None,
        "stage": "start",
        "progress": 2,
        "trigger": trigger,
    })
    try:
        threading.Thread(
            target=scan_worker,
            args=(session_tags, trigger, overrides or {}),
            daemon=True,
        ).start()
    except RuntimeError:
        # Without this the lock would stay held and block every later scan.
        scan_state["running"] = False
        scan_lock.release()
        raise
    return True


def scan_config(session_tags: list[str], overrides: dict[str, Any]) -> dict[str, Any]:
    config = store.get()
    for key in SCAN_OVERRIDES:
        if key in overrides and overrides[key] not in (None, ""):
            config[key] = overrides[key]
    config = validate_config(config)
    if session_tags:
        config["default_tags"] = list(dict.fromkeys(config["default_tags"] + session_tags))
        logs.publish(f"Session-Tags: {', '.join(session_tags)}")
    return config


def scan_worker(session_tags: list[str], trigger: str, overrides: dict[str, Any]) -> None:
    try:
        config = scan_config(session_tags, overrides)
        logs.publish(f"Scan angefordert ({'Home Assistant' if trigger == 'ha' else 'Oberfläche'}).")
        logs.progress("start", 4)
        if config["upload_to_paperless"]:
            PaperlessClient(config, logs).require_config()
        sheets = ScannerClient(config, logs, timings).scan()

        if batch.active():
            # Collect the sheets; nothing is uploaded until the batch is closed.
            index, total, replaced, added = batch.add(sheets)
            action = (f"Seite {index + 1} ersetzt" if replaced and added == 1
                      else f"Seite {index + 1} durch {added} Seiten ersetzt" if replaced
                      else f"{added} Seiten aufgenommen" if added > 1
                      else f"Seite {total} aufgenommen")
            logs.publish(f"{action} · {total} Seite(n) im Stapel.", "success")
            logs.progress("done", 100, batch=True, pages=total, page_index=index, added=added)
            notify_home_assistant(config, "page", f"Seite {index + 1}", None)
            return

        target, count = collect_document(sheets, config)
        remember_result(target, "pdf" if target.suffix.lower() == ".pdf" else "image")
        if config["upload_to_paperless"]:
            logs.progress("upload", 88)
        else:
            logs.publish("Upload nach Paperless-ngx ist deaktiviert.", "warning")
        queue_upload(target, config, count, config["default_tags"])
        logs.progress("done", 100, file=target.name, pages=count)
        logs.publish("Workflow abgeschlossen.", "success")
        notify_home_assistant(config, "success", target.name, None)
    except Exception as error:  # surface every device/API failure in the log stream
        scan_state["last_error"] = str(error)
        logs.progress("error", 100, error=str(error))
        logs.publish(f"Workflow fehlgeschlagen: {error}", "error")
        notify_home_assistant(store.get(), "error", None, str(error))
    finally:
        scan_state["running"] = False
        scan_lock.release()


def collect_document(sheets: list[Path], config: dict[str, Any]) -> tuple[Path, int]:
    """One scan run becomes one document, however many sheets it took."""
    if len(sheets) == 1:
        return sheets[0], page_count(sheets[0])

    logs.progress("merge", 84)
    logs.publish(f"Füge {len(sheets)} Blatt zu einem PDF zusammen …")
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    target = Path(config["output_dir"]) / f"scan_{timestamp}_{len(sheets)}-seiten.pdf"
    count = merge_files(sheets, target)
    logs.publish(f"Dokument erstellt: {target.name} ({count} Seiten)", "success")
    return target, count


def notify_home_assistant(config: dict[str, Any], status: str, filename: str | None, error: str | None) -> None:
    """Fire-and-forget callback so HA automations can react to a finished scan."""
    webhook = config.get("ha_webhook_url")
    if not config.get("ha_enabled") or not webhook:
        return

    def send() -> None:
        try:
            requests.post(
                webhook,
                json={
                    "status": status,
                    "file": filename,
                    "error": error,
                    "trigger": scan_state.get("trigger"),
                    "finished": datetime.now().isoformat(timespec="seconds"),
                },
                timeout=10,
            )
            logs.publish("Home Assistant benachrichtigt.", "info")
        except requests.RequestException as request_error:
            logs.publish(f"Home-Assistant-Webhook fehlgeschlagen: {request_error}", "warning")

    threading.Thread(target=send, daemon=True).start()


def batch_finish_worker(session_tags: list[str], metadata: dict[str, Any] | None = None) -> None:
    try:
        config = validate_config(store.get())
        config.update(metadata or {})
        target = finish_batch(config, session_tags)
        logs.progress("done", 100, file=target.name)
        logs.publish("Stapel abgeschlossen.", "success")
        notify_home_assistant(config, "success", target.name, None)
    except Exception as error:
        scan_state["last_error"] = str(error)
        logs.progress("error", 100, error=str(error))
        logs.publish(f"Stapel fehlgeschlagen: {error}", "error")
        notify_home_assistant(store.get(), "error", None, str(error))
    finally:
        scan_state["running"] = False
        scan_lock.release()


def start_batch_finish(
    session_tags: list[str], metadata: dict[str, Any], trigger: str
) -> tuple[str, int] | None:
    """Close the batch in the background. Returns (message, status) on refusal.

    Shared by the interface and Home Assistant so both take the same lock and
    cannot merge the same pages twice.
    """
    if not batch.count():
        return "Der Stapel enthält keine Seiten.", 400
    if not scan_lock.acquire(blocking=False):
        return "Ein Scan läuft gerade.", 409
    scan_state.update({"running": True, "last_error": None, "stage": "merge",
                       "progress": 10, "trigger": trigger})
    try:
        threading.Thread(target=batch_finish_worker, args=(session_tags, metadata), daemon=True).start()
    except RuntimeError:
        scan_state["running"] = False
        scan_lock.release()
        raise
    return None


# --------------------------------------------------------------------------- #
# Response helpers
# --------------------------------------------------------------------------- #

def preview_response(path: Path, rotation: int = 0) -> Response:
    """Rasterised page, or the untouched file when rendering is not possible."""
    try:
        payload, mimetype = render_preview(path, rotation)
    except Exception as error:  # missing wheel or broken PDF: fall back to the file
        logs.publish(f"Vorschau nicht gerendert ({error}); zeige Originaldatei.", "warning")
        return send_file(path, mimetype="application/pdf", max_age=0)
    return Response(payload, mimetype=mimetype, headers={"Cache-Control": "no-store"})


def scan_file_response(path: Path) -> Response:
    mimetype = "application/pdf" if path.suffix.lower() == ".pdf" else "image/jpeg"
    return send_file(path, mimetype=mimetype, as_attachment=False, download_name=path.name, max_age=0)


def storage_error(error: OSError, target: Path) -> tuple[Response, int]:
    """Turn a bare PermissionError into something a user can act on."""
    message = (
        f"{target} ist nicht beschreibbar ({error.strerror or error}). "
        "Der Ordner gehoert vermutlich einem anderen Benutzer als dem Dienst — "
        "Container neu starten oder PUID/PGID passend zum Host setzen."
    )
    logs.publish(message, "error")
    return jsonify({"error": message}), 500


def parse_session_tags(raw: Any) -> list[str]:
    """Tags for this one scan: a list, or a comma separated string from HA."""
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(str(tag).strip() for tag in raw if str(tag).strip()))


# --------------------------------------------------------------------------- #
# Routes: shell and settings
# --------------------------------------------------------------------------- #

@app.get("/")
def index() -> str:
    return render_template("index.html", version=APP_VERSION)


@app.get("/manifest.webmanifest")
def manifest() -> Response:
    response = send_from_directory(app.static_folder, "manifest.webmanifest")
    response.headers["Content-Type"] = "application/manifest+json"
    return response


@app.get("/sw.js")
def service_worker() -> Response:
    # Served from the root so the worker can control the whole origin, and
    # rendered so the cache name carries the version: without that an installed
    # app keeps the stylesheet it was first installed with.
    response = Response(render_template("sw.js", version=APP_VERSION))
    response.headers["Content-Type"] = "application/javascript"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/api/config")
def get_config() -> Response:
    config = store.public()
    config["suggested_subnet"] = config.get("discovery_subnet") or guess_local_subnet()
    config["version"] = APP_VERSION
    config["releases_url"] = RELEASES_URL
    config["paper_sizes"] = list(PAPER_SIZES)
    return jsonify(config)


@app.put("/api/config")
def put_config() -> Response:
    try:
        config = store.save(request.get_json(force=True) or {})
        # A freshly chosen folder is created and probed now, so a wrong path is
        # reported here instead of failing halfway through the next scan.
        check_writable(Path(config["output_dir"]), "Scan-Ablage")
        logs.publish("Einstellungen gespeichert.", "success")
        return jsonify(config)
    except (TypeError, ValueError) as error:
        return jsonify({"error": str(error)}), 400
    except OSError as error:
        return storage_error(error, Path(getattr(error, "filename", None) or CONFIG_PATH))


@app.post("/api/setup/complete")
def complete_setup() -> Response:
    try:
        payload = request.get_json(silent=True) or {}
        if payload:
            store.save(payload)
        config = store.patch(setup_complete=True)
        logs.publish("Einrichtung abgeschlossen.", "success")
        return jsonify(config)
    except (TypeError, ValueError) as error:
        return jsonify({"error": str(error)}), 400
    except OSError as error:
        return storage_error(error, Path(getattr(error, "filename", None) or CONFIG_PATH))


@app.post("/api/setup/reset")
def reset_setup() -> Response:
    """Wipe the stored configuration and restart the wizard."""
    try:
        public_config = store.reset()
    except OSError as error:
        return jsonify({"error": str(error)}), 500
    logs.publish("Konfiguration zurückgesetzt.", "warning")
    return jsonify(public_config)


@app.get("/api/state")
def state() -> Response:
    return jsonify({
        **scan_state,
        "setup_complete": store.get()["setup_complete"],
        "batch": batch.public(),
        "queue": jobs.pending_count(),
    })


# --------------------------------------------------------------------------- #
# Routes: devices
# --------------------------------------------------------------------------- #

@app.post("/api/test/scanner")
def test_scanner() -> Response:
    try:
        result = ScannerClient(store.get(), logs, timings).capabilities()
        return jsonify({"ok": True, **result})
    except (requests.RequestException, ET.ParseError, RuntimeError) as error:
        logs.publish(f"Scanner-Test fehlgeschlagen: {error}", "error")
        return jsonify({"ok": False, "error": str(error)}), 502


@app.post("/api/test/paperless")
def test_paperless() -> Response:
    try:
        PaperlessClient(store.get(), logs).test()
        return jsonify({"ok": True})
    except (requests.RequestException, RuntimeError) as error:
        logs.publish(f"Paperless-Test fehlgeschlagen: {error}", "error")
        return jsonify({"ok": False, "error": str(error)}), 502


@app.post("/api/discover/scanners")
def discover_scanners() -> Response:
    """Search one given network, or auto-detect the likely ones when none is set."""
    try:
        payload = request.get_json(silent=True) or {}
        config = store.get()
        subnet = str(payload.get("discovery_subnet") or "").strip()

        if subnet and not payload.get("auto"):
            config["discovery_subnet"] = subnet
            devices = discover_escl_scanners(config, logs)
            return jsonify({"ok": True, "devices": devices, "subnet": config["discovery_subnet"]})

        devices, found_in = autodiscover_scanners(config, logs, request.remote_addr)
        if not devices:
            logs.publish("Kein Scanner gefunden. Netzwerk bitte manuell angeben.", "warning")
        return jsonify({"ok": True, "devices": devices, "subnet": found_in, "auto": True})
    except (ValueError, requests.RequestException) as error:
        logs.publish(f"Scanner-Suche fehlgeschlagen: {error}", "error")
        return jsonify({"ok": False, "error": str(error)}), 400


@app.get("/api/discover/candidates")
def discovery_candidates() -> Response:
    return jsonify({"candidates": candidate_subnets(store.get(), request.remote_addr)[:4]})


@app.post("/api/scanner/prewarm")
def prewarm() -> Response:
    """Nudge the scanner awake while the user is still choosing settings."""
    config = store.get()
    if not config.get("prewarm_enabled") or not config.get("scanner_url") or scan_state["running"]:
        return jsonify({"ok": False, "skipped": True})

    def wake() -> None:
        try:
            ScannerClient(config, logs, timings).status()
        except Exception:
            pass  # a sleeping or missing scanner must not produce noise

    threading.Thread(target=wake, daemon=True).start()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# Routes: scanning
# --------------------------------------------------------------------------- #

@app.post("/api/scan")
def start_scan() -> Response:
    payload = request.get_json(silent=True) or {}
    session_tags = parse_session_tags(payload.get("session_tags"))
    if not start_scan_job(session_tags, "ui", payload.get("overrides")):
        return jsonify({"error": "Ein Scan läuft bereits."}), 409
    return jsonify({"ok": True, "message": "Scan wurde gestartet."}), 202


@app.get("/api/batch")
def get_batch() -> Response:
    return jsonify(batch.public())


@app.post("/api/batch/start")
def start_batch() -> Response:
    public_batch = batch.begin()
    logs.publish("Stapel gestartet — Seiten werden gesammelt.", "success")
    return jsonify(public_batch)


@app.post("/api/batch/cancel")
def cancel_batch() -> Response:
    pages = batch.count()
    batch.clear()
    logs.publish(f"Stapel verworfen ({pages} Seite(n)).", "warning")
    return jsonify(batch.public())


@app.post("/api/batch/replace")
def arm_replace() -> Response:
    """Arm the next scan to overwrite one page instead of appending."""
    index = (request.get_json(silent=True) or {}).get("index")
    if not batch.arm(index):
        return jsonify({"error": "Diese Seite gibt es nicht."}), 400
    if index is not None:
        logs.publish(f"Nächster Scan ersetzt Seite {int(index) + 1}.")
    return jsonify(batch.public())


@app.delete("/api/batch/page/<int:index>")
def delete_batch_page(index: int) -> Response:
    if not batch.remove(index):
        return jsonify({"error": "Diese Seite gibt es nicht."}), 404
    logs.publish(f"Seite {index + 1} aus dem Stapel entfernt.")
    return jsonify(batch.public())


@app.get("/api/batch/page/<int:index>/preview")
def batch_page_preview(index: int) -> Response:
    page = batch.page(index)
    if not page:
        return jsonify({"error": "Diese Seite gibt es nicht."}), 404
    path = Path(page["path"])
    if not path.exists():
        return jsonify({"error": "Seite nicht mehr vorhanden."}), 404
    return preview_response(path, page.get("rotation", 0))


@app.post("/api/batch/page/<int:index>/rotate")
def rotate_batch_page(index: int) -> Response:
    """Turn a single page; the rotation is applied when the batch is merged."""
    degrees = normalise_rotation((request.get_json(silent=True) or {}).get("degrees", 90)) or 90
    if not batch.rotate(index, degrees):
        return jsonify({"error": "Diese Seite gibt es nicht."}), 404
    return jsonify(batch.public())


@app.post("/api/batch/order")
def reorder_batch() -> Response:
    """Apply a new page order, e.g. after dragging a page to another slot."""
    order = (request.get_json(silent=True) or {}).get("order")
    if not batch.reorder(order):
        return jsonify({"error": "Die Reihenfolge passt nicht zum Stapel."}), 400
    logs.publish("Reihenfolge im Stapel geändert.")
    return jsonify(batch.public())


@app.post("/api/batch/finish")
def close_batch() -> Response:
    payload = request.get_json(silent=True) or {}
    metadata = {key: payload[key] for key in ("correspondent", "document_type")
                if payload.get(key) not in (None, "")}
    refused = start_batch_finish(parse_session_tags(payload.get("session_tags")), metadata, "batch")
    if refused:
        message, status = refused
        return jsonify({"error": message}), status
    return jsonify({"ok": True}), 202


# --------------------------------------------------------------------------- #
# Routes: results
# --------------------------------------------------------------------------- #

@app.get("/api/preview")
def preview() -> Response:
    """Rasterised view of the most recent scan for the post-scan preview."""
    last_file = scan_state.get("last_file")
    if not last_file or not Path(last_file).exists():
        return jsonify({"error": "Keine Vorschau verfügbar."}), 404
    return preview_response(Path(last_file))


@app.get("/api/preview/file")
def preview_file() -> Response:
    last_file = scan_state.get("last_file")
    if not last_file or not Path(last_file).exists():
        return jsonify({"error": "Keine Datei verfügbar."}), 404
    return scan_file_response(Path(last_file))


@app.get("/api/history")
def get_history() -> Response:
    limit = max(1, min(200, int(request.args.get("limit", 40))))
    entries = jobs.list(limit)
    for entry in entries:
        entry["exists"] = (not entry.get("file_deleted")) and Path(entry["path"]).exists()
        entry.pop("path", None)
    return jsonify({"jobs": entries, "open": jobs.pending_count()})


@app.get("/api/history/<job_id>/preview")
def history_preview(job_id: str) -> Response:
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Unbekannter Eintrag."}), 404
    path = Path(job["path"])
    if not path.exists():
        return jsonify({"error": "Datei wurde bereits aufgeräumt."}), 404
    return preview_response(path)


@app.get("/api/history/<job_id>/file")
def history_file(job_id: str) -> Response:
    job = jobs.get(job_id)
    if not job or not Path(job["path"]).exists():
        return jsonify({"error": "Datei wurde bereits aufgeräumt."}), 404
    return scan_file_response(Path(job["path"]))


@app.post("/api/history/<job_id>/retry")
def history_retry(job_id: str) -> Response:
    """Send a document to Paperless again — after a failure or on purpose."""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Unbekannter Eintrag."}), 404
    if not Path(job["path"]).exists():
        return jsonify({"error": "Die Datei wurde bereits aufgeräumt."}), 410
    jobs.update(job_id, status="pending", next_attempt=0, error=None, attempts=0)
    threading.Thread(target=attempt_upload, args=(job_id,), daemon=True).start()
    return jsonify({"ok": True}), 202


@app.delete("/api/history/<job_id>")
def history_delete(job_id: str) -> Response:
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Unbekannter Eintrag."}), 404
    # Remove the file first: dropping the entry on a failed unlink would leave
    # a scan on disk that nothing in the interface can reach any more.
    try:
        Path(job["path"]).unlink(missing_ok=True)
    except OSError as error:
        return jsonify({"error": str(error)}), 500
    jobs.remove(job_id)
    logs.publish(f"{job['name']} aus dem Verlauf gelöscht.")
    return jsonify({"ok": True})


@app.get("/api/paperless/collections")
def paperless_collections() -> Response:
    """Tags, correspondents and document types for the pickers."""
    try:
        return jsonify({"ok": True, **PaperlessClient(store.get(), logs).collections()})
    except (requests.RequestException, RuntimeError) as error:
        return jsonify({"ok": False, "error": str(error)}), 502


# --------------------------------------------------------------------------- #
# Routes: stream, update, health
# --------------------------------------------------------------------------- #

@app.get("/api/logs")
def stream_logs() -> Response:
    try:
        stream = logs.stream()
    except TooManySubscribers:
        # Better an honest refusal than a request that hangs forever because
        # every worker thread is parked on a stream.
        return jsonify({"error": "Zu viele offene Verbindungen."}), 503
    return Response(
        stream_with_context(stream),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/update")
def update_info() -> Response:
    """Version banner data; never blocks the UI when GitHub is unreachable."""
    if not store.get().get("update_check"):
        return jsonify({
            "current": APP_VERSION,
            "latest": "",
            "update_available": False,
            "url": RELEASES_URL,
            "disabled": True,
        })
    return jsonify(check_for_update(force=request.args.get("force") == "1"))


@app.get("/health")
def health() -> Response:
    return jsonify({"ok": True, "version": APP_VERSION})


# --------------------------------------------------------------------------- #
# Routes: access protection
# --------------------------------------------------------------------------- #

@app.get("/api/auth/state")
def auth_state() -> Response:
    return jsonify(auth.public_state(store.get(), is_authenticated()))


@app.post("/api/auth/login")
def auth_login() -> Response:
    config = store.get()
    if not config.get("auth_enabled"):
        return jsonify({"ok": True, **auth.public_state(config, True)})

    blocked = login_throttle.blocked_seconds()
    if blocked:
        return jsonify({
            "ok": False,
            "error": f"Zu viele Fehlversuche. Bitte {blocked // 60 + 1} Minuten warten.",
            "retry_after": blocked,
        }), 429

    password = str((request.get_json(silent=True) or {}).get("password", ""))
    if not auth.verify_password(config.get("auth_password_hash", ""), password):
        login_throttle.record_failure()
        logs.publish("Anmeldung mit falschem Passwort abgelehnt.", "warning")
        return jsonify({"ok": False, "error": "Passwort stimmt nicht."}), 401

    login_throttle.reset()
    sign_in()
    return jsonify({"ok": True, **auth.public_state(config, True)})


@app.post("/api/auth/logout")
def auth_logout() -> Response:
    session.pop("auth", None)
    return jsonify({"ok": True})


@app.post("/api/auth/enable")
def auth_enable() -> Response:
    """Turn protection on and log this browser in, so nobody locks themselves out."""
    payload = request.get_json(silent=True) or {}
    try:
        password = auth.check_password_rules(payload.get("password", ""))
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    try:
        store.patch(auth_password_hash=auth.hash_password(password), auth_enabled=True)
    except OSError as error:
        return storage_error(error, CONFIG_PATH)
    sign_in()
    logs.publish("Zugriffsschutz eingeschaltet.", "success")
    return jsonify({"ok": True, **auth.public_state(store.get(), True)})


@app.post("/api/auth/password")
def auth_change_password() -> Response:
    """Change the password. Only reachable with a valid session."""
    payload = request.get_json(silent=True) or {}
    config = store.get()
    if config.get("auth_enabled") and not auth.verify_password(
        config.get("auth_password_hash", ""), str(payload.get("current", ""))
    ):
        return jsonify({"ok": False, "error": "Bisheriges Passwort stimmt nicht."}), 401
    try:
        password = auth.check_password_rules(payload.get("password", ""))
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    try:
        store.patch(auth_password_hash=auth.hash_password(password), auth_enabled=True)
    except OSError as error:
        return storage_error(error, CONFIG_PATH)
    sign_in()  # every other browser is signed out by the changed password
    logs.publish("Passwort für den Zugriffsschutz geändert.", "success")
    return jsonify({"ok": True, **auth.public_state(store.get(), True)})


@app.post("/api/auth/disable")
def auth_disable() -> Response:
    try:
        store.patch(auth_enabled=False, auth_password_hash="")
    except OSError as error:
        return storage_error(error, CONFIG_PATH)
    session.pop("auth", None)
    login_throttle.reset()
    logs.publish("Zugriffsschutz ausgeschaltet.", "warning")
    return jsonify({"ok": True, **auth.public_state(store.get(), True)})


# --------------------------------------------------------------------------- #
# Home Assistant integration
# --------------------------------------------------------------------------- #

def require_api_key(view: Callable[..., Any]) -> Callable[..., Any]:
    # An automation authenticates with its key, not with a browser session, so
    # switching the interface protection on must not break it.
    API_KEY_ROUTES.add(view.__name__)

    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        config = store.get()
        if not config.get("ha_enabled"):
            return jsonify({"error": "Home-Assistant-Schnittstelle ist deaktiviert."}), 403
        expected = config.get("ha_api_key", "")
        provided = (
            request.headers.get("X-API-Key")
            or request.args.get("api_key")
            or (request.headers.get("Authorization", "").removeprefix("Bearer ").strip())
        )
        if not expected or not provided or not secrets.compare_digest(str(provided), str(expected)):
            return jsonify({"error": "Ungültiger API-Key."}), 401
        return view(*args, **kwargs)

    return wrapper


@app.post("/api/ha/key")
def rotate_ha_key() -> Response:
    """Generate a fresh API key for Home Assistant and return it once."""
    key = secrets.token_urlsafe(32)
    store.patch(ha_api_key=key, ha_enabled=True)
    logs.publish("Neuer Home-Assistant-API-Key erzeugt.", "success")
    return jsonify({"ok": True, "api_key": key})


@app.get("/api/ha/key")
def read_ha_key() -> Response:
    return jsonify({"api_key": store.get().get("ha_api_key", "")})


@app.post("/api/ha/scan")
@require_api_key
def ha_scan() -> Response:
    """Trigger endpoint for HA automations (motion sensor, button, script, …)."""
    payload = request.get_json(silent=True) or {}
    session_tags = parse_session_tags(payload.get("tags") or payload.get("session_tags"))
    overrides = {key: payload[key] for key in SCAN_OVERRIDES if key in payload}
    if not start_scan_job(session_tags, "ha", overrides):
        return jsonify({"ok": False, "error": "Ein Scan läuft bereits."}), 409
    return jsonify({"ok": True, "message": "Scan gestartet."}), 202


@app.get("/api/ha/state")
@require_api_key
def ha_state() -> Response:
    """Flat payload for a Home Assistant RESTful sensor."""
    config = store.get()
    return jsonify({
        "state": "scanning" if scan_state["running"] else ("error" if scan_state["last_error"] else "idle"),
        "running": scan_state["running"],
        "stage": scan_state["stage"],
        "progress": scan_state["progress"],
        "last_file": scan_state["last_name"],
        "last_finished": scan_state["last_finished"],
        "last_error": scan_state["last_error"],
        "trigger": scan_state["trigger"],
        "batch_active": batch.active(),
        "batch_pages": batch.count(),
        "queue": jobs.pending_count(),
        "version": APP_VERSION,
        "scanner_url": config["scanner_url"],
        "upload_to_paperless": config["upload_to_paperless"],
    })


@app.post("/api/ha/batch")
@require_api_key
def ha_batch() -> Response:
    """Let an automation open, close or discard a batch (e.g. two buttons)."""
    action = str((request.get_json(silent=True) or {}).get("action", "")).lower()
    if action == "start":
        public_batch = batch.begin()
        logs.publish("Stapel über Home Assistant gestartet.", "success")
        return jsonify({"ok": True, **public_batch})
    if action == "cancel":
        batch.clear()
        logs.publish("Stapel über Home Assistant verworfen.", "warning")
        return jsonify({"ok": True, **batch.public()})
    if action == "finish":
        refused = start_batch_finish([], {}, "ha")
        if refused:
            message, status = refused
            return jsonify({"ok": False, "error": message}), status
        return jsonify({"ok": True, "message": "Stapel wird abgeschlossen."}), 202
    return jsonify({"ok": False, "error": "action muss start, finish oder cancel sein."}), 400


@app.post("/api/ha/test")
@require_api_key
def ha_test() -> Response:
    logs.publish("Home Assistant hat die Verbindung getestet.", "success")
    return jsonify({"ok": True, "message": "Verbindung steht."})


# Erst starten, wenn alle Funktionen definiert sind.
threading.Thread(target=queue_worker, daemon=True, name="scandeck-queue").start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)
