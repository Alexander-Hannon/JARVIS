"""Google Drive adapter (DriveProvider), strictly read-only.

See google_oauth module docstring for the drive.readonly vs drive.file trade-off. This adapter has no
mutation methods. The application layer must pass only folder ids inside AJ's selected-root allowlist;
``search`` refuses to run without folder ids (never searches all of Drive) and matches direct children
of the given folders only, so callers pass every allowlisted folder they want searched.

Verified 2026-09-28 (https://developers.google.com/workspace/drive/api/reference/rest/v3):
``files.get`` (``alt=media`` downloads blob content), ``files.list`` with ``q`` / ``pageToken``,
``files.export`` for Google Workspace documents (export output is limited to 10 MB by Google).
"""
from __future__ import annotations

import re
from typing import Any

import httpx

from .base import ProviderError
from .google_oauth import GoogleCredentials, google_api_error, transport_error

API_BASE = "https://www.googleapis.com/drive/v3"
FILE_FIELDS = "id,name,mimeType,modifiedTime,md5Checksum,size,parents,webViewLink,trashed,version,driveId"
FOLDER_MIME = "application/vnd.google-apps.folder"
EXPORT_MIME = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "application/pdf",
}
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


def _check_id(file_id: str) -> str:
    if not _ID_RE.match(file_id or ""):
        raise ProviderError("validation", f"invalid Drive id {file_id!r}")
    return file_id


def escape_query_string(value: str) -> str:
    """Escape a literal for a Drive ``q`` single-quoted string (backslash, then single quote)."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDriveAdapter:
    name = "google_drive"
    simulated = False

    def __init__(self, credentials: GoogleCredentials, *, base_url: str = API_BASE):
        self._creds = credentials
        self._base = base_url.rstrip("/")

    def get_file(self, file_id: str) -> dict:
        return self._creds.request("GET", f"{self._base}/files/{_check_id(file_id)}",
                                   params={"fields": FILE_FIELDS, "supportsAllDrives": "true"}).json()

    def _list(self, q: str) -> list[dict]:
        out: list[dict] = []
        token: str | None = None
        for _ in range(100):
            params: dict[str, Any] = {"q": q, "fields": f"nextPageToken,files({FILE_FIELDS})", "pageSize": 1000,
                                      "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
            if token:
                params["pageToken"] = token
            body = self._creds.request("GET", f"{self._base}/files", params=params).json()
            out.extend(body.get("files") or [])
            token = body.get("nextPageToken")
            if not token:
                return out
        raise ProviderError("server", "Drive pagination did not terminate")

    def list_folder(self, folder_id: str) -> list[dict]:
        return self._list(f"'{_check_id(folder_id)}' in parents and trashed=false")

    def search(self, query: str, folder_ids: list[str]) -> list[dict]:
        if not folder_ids:
            return []  # never search outside selected roots
        if not query.strip():
            raise ProviderError("validation", "empty search query")
        parents = " or ".join(f"'{_check_id(f)}' in parents" for f in folder_ids)
        return self._list(f"fullText contains '{escape_query_string(query)}' and trashed=false and ({parents})")

    def download(self, file_id: str, mime_type: str, max_bytes: int) -> tuple[bytes, str]:
        _check_id(file_id)
        if mime_type in EXPORT_MIME:
            target = EXPORT_MIME[mime_type]
            url, params = f"{self._base}/files/{file_id}/export", {"mimeType": target}
        elif mime_type.startswith("application/vnd.google-apps."):
            raise ProviderError("unsupported", f"cannot download Google Workspace type {mime_type}")
        else:
            target = None
            url, params = f"{self._base}/files/{file_id}", {"alt": "media", "supportsAllDrives": "true"}
        for attempt in (0, 1):
            headers = {"Authorization": f"Bearer {self._creds.access_token()}"}
            try:
                with self._creds.client.stream("GET", url, params=params, headers=headers) as resp:
                    if resp.status_code == 401 and attempt == 0:
                        self._creds.refresh()
                        continue
                    if resp.status_code >= 400:
                        resp.read()
                        raise google_api_error(resp, write=False)
                    declared = resp.headers.get("Content-Length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise ProviderError("validation", f"file exceeds max_bytes ({declared} > {max_bytes})",
                                            detail={"size": int(declared), "max_bytes": max_bytes})
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in resp.iter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise ProviderError("validation", f"file exceeds max_bytes ({max_bytes})",
                                                detail={"max_bytes": max_bytes})
                        chunks.append(chunk)
                    effective = target or (resp.headers.get("Content-Type") or mime_type).split(";")[0].strip()
                    return b"".join(chunks), effective
            except httpx.TransportError as exc:
                raise transport_error(exc, write=False) from exc
        raise ProviderError("auth", "Drive download unauthorized after refresh")
