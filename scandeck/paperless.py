"""Handing a finished document to Paperless-ngx and following what became of it."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from scandeck.events import LogHub


class PaperlessClient:
    def __init__(self, config: dict[str, Any], log: LogHub) -> None:
        self.config = config
        self.log = log
        self.base_url = config["paperless_url"].rstrip("/")
        self.headers = {"Authorization": f"Token {config['paperless_token']}"}

    def require_config(self) -> None:
        """Fail early — before the scanner is asked to pull in a sheet."""
        if not self.base_url or not self.config.get("paperless_token"):
            raise RuntimeError("Paperless-URL oder API-Token fehlt.")

    def test(self) -> None:
        self.require_config()
        response = requests.get(f"{self.base_url}/api/documents/?page_size=1", headers=self.headers, timeout=20)
        response.raise_for_status()
        self.log.publish("Paperless-ngx erreichbar und Token akzeptiert.", "success")

    def upload(self, file_path: Path) -> str:
        """Hand the file to Paperless and return the task id it answers with."""
        self.require_config()
        tag_ids = self._resolve_tags()
        data: list[tuple[str, str]] = [("title", f"{self.config['title_prefix']} {datetime.now():%Y-%m-%d %H:%M}")]
        data.extend(("tags", str(tag_id)) for tag_id in tag_ids)
        for field in ("correspondent", "document_type"):
            value = self.config.get(field)
            if value:
                data.append((field, str(value)))
        self.log.publish(f"Lade {file_path.name} nach Paperless-ngx hoch …")
        with file_path.open("rb") as document:
            response = requests.post(
                f"{self.base_url}/api/documents/post_document/",
                headers=self.headers,
                data=data,
                files={"document": (file_path.name, document)},
                timeout=90,
            )
        response.raise_for_status()
        task_id = response.text.strip().strip('"')
        self.log.publish(f"Paperless-ngx hat {file_path.name} angenommen, Verarbeitung läuft …", "success")
        return task_id

    def task_state(self, task_id: str) -> dict[str, Any]:
        """Ask what became of an upload: accepted, duplicate or failed.

        Paperless 3.x renamed these fields, so both spellings are accepted.
        """
        response = requests.get(f"{self.base_url}/api/tasks/?task_id={task_id}", headers=self.headers, timeout=20)
        response.raise_for_status()
        body = response.json()
        items = body.get("results", body) if isinstance(body, dict) else body
        if not items:
            return {"status": "unknown", "document_id": None, "message": "", "duplicate": False}
        task = items[0]
        status = str(task.get("status", "")).lower()
        documents = task.get("related_document_ids") or []
        single = task.get("related_document")
        if single and not documents:
            documents = [single]
        message = str(task.get("result_data") or task.get("result") or task.get("status_display") or "")
        return {
            "status": status,
            "document_id": documents[0] if documents else None,
            "message": message,
            "duplicate": "duplicate" in message.lower() or "bereits" in message.lower(),
        }

    def collections(self) -> dict[str, list[dict[str, Any]]]:
        """Tags, correspondents and document types for the pickers."""
        self.require_config()
        result: dict[str, list[dict[str, Any]]] = {}
        for key, endpoint, ordering in (
            ("tags", "tags", "-document_count"),
            ("correspondents", "correspondents", "-document_count"),
            ("document_types", "document_types", "name"),
        ):
            response = requests.get(
                f"{self.base_url}/api/{endpoint}/?page_size=100&ordering={ordering}",
                headers=self.headers,
                timeout=20,
            )
            response.raise_for_status()
            body = response.json()
            items = body.get("results", body) if isinstance(body, dict) else body
            result[key] = [
                {"id": item.get("id"), "name": item.get("name"), "count": item.get("document_count", 0)}
                for item in items
                if item.get("name")
            ]
        return result

    def _resolve_tags(self) -> list[int]:
        wanted = self.config["default_tags"]
        if not wanted:
            return []
        response = requests.get(f"{self.base_url}/api/tags/?page_size=100", headers=self.headers, timeout=20)
        response.raise_for_status()
        body = response.json()
        known = body.get("results", body) if isinstance(body, dict) else body
        by_name = {str(tag.get("name", "")).casefold(): tag.get("id") for tag in known}
        tag_ids: list[int] = []
        for tag_name in wanted:
            tag_id = by_name.get(tag_name.casefold())
            if tag_id is None and self.config["create_missing_tags"]:
                created = requests.post(
                    f"{self.base_url}/api/tags/",
                    headers={**self.headers, "Content-Type": "application/json"},
                    json={"name": tag_name, "color": "#c99b52"},
                    timeout=20,
                )
                created.raise_for_status()
                tag_id = created.json().get("id")
                self.log.publish(f"Paperless-Tag erstellt: {tag_name}")
            if tag_id is None:
                self.log.publish(f"Tag fehlt in Paperless und wurde übersprungen: {tag_name}", "warning")
            else:
                tag_ids.append(int(tag_id))
        return tag_ids
