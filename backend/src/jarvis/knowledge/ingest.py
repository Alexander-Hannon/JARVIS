"""Local document ingestion: originals preserved content-addressed, versions, text + visual segments.

Uploaded content is untrusted DATA. Nothing here interprets, follows, or executes document
content (no notebook execution, no macros, no embedded scripts). Text goes into FTS for
retrieval; visual content is kept as image segments so it can be sent to a vision model.

Storage layout (relative paths are stored in the DB, resolved against settings.files_dir):
  <sha[:2]>/<sha>                   original bytes, content-addressed
  derived/<sha[:2]>/<sha>.<ext>     model-input image copies (long edge <= 1568px)
"""
from __future__ import annotations

import base64
import io
import json
import re
import sqlite3
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from jarvis.config import get_settings
from jarvis.core.db import jdump, jload, row, rows
from jarvis.core.util import audit, new_id, now_iso, sha256_bytes

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 50
MODEL_IMAGE_MAX_EDGE = 1568
LOW_TEXT_CHARS = 200            # a PDF page with less text than this is rendered for vision
VECTOR_PATH_THRESHOLD = 50      # many path objects on a page ~ a drawn diagram/chart
MAX_SEGMENT_CHARS = 4000
MAX_MODEL_IMAGE_BYTES = 3_700_000  # base64 of this stays under the API's 5 MB per-image limit

IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_EXT_KIND = {
    ".txt": "text", ".text": "text", ".md": "markdown", ".markdown": "markdown",
    ".pdf": "pdf", ".docx": "docx", ".ipynb": "ipynb",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image", ".gif": "image",
}
_MIME_KIND = {
    "text/plain": "text", "text/markdown": "markdown", "text/x-markdown": "markdown",
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/x-ipynb+json": "ipynb",
    **{m: "image" for m in IMAGE_MIMES},
}
_KIND_MIME = {
    "text": "text/plain", "markdown": "text/markdown", "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "ipynb": "application/x-ipynb+json",
}


# ---------------------------------------------------------------- storage helpers

def files_root() -> Path:
    return get_settings().files_dir


def resolve_path(rel: str) -> Path:
    """Resolve a stored relative path; refuses anything escaping files_dir."""
    root = files_root().resolve()
    p = (root / rel).resolve()
    if root != p and root not in p.parents:
        raise ValueError("stored path escapes files_dir")
    return p


def _write_blob(rel: str, data: bytes) -> None:
    p = resolve_path(rel)
    if p.exists():
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(p)


def store_original(data: bytes, sha: str | None = None) -> str:
    sha = sha or sha256_bytes(data)
    rel = f"{sha[:2]}/{sha}"
    _write_blob(rel, data)
    return rel


def _store_derived(data: bytes, ext: str) -> str:
    sha = sha256_bytes(data)
    rel = f"derived/{sha[:2]}/{sha}.{ext}"
    _write_blob(rel, data)
    return rel


def make_model_image(raw: bytes) -> tuple[bytes, str, int, int]:
    """Return a model-input copy (long edge <= 1568px) as (bytes, mime, w, h). Original is untouched.

    JPEG stays JPEG; other formats become PNG (first frame for animations). Oversized PNGs fall
    back to JPEG so the base64 payload stays within API per-image limits.
    """
    from PIL import Image

    with Image.open(io.BytesIO(raw)) as im:
        fmt = (im.format or "").upper()
        im.seek(0)
        im.load()
        w, h = im.size
        scale = min(1.0, MODEL_IMAGE_MAX_EDGE / max(w, h))
        out = im
        if scale < 1.0:
            out = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        if fmt == "JPEG" and scale == 1.0 and len(raw) <= MAX_MODEL_IMAGE_BYTES and out.mode in ("RGB", "L"):
            return raw, "image/jpeg", w, h
        if fmt == "JPEG":
            return _to_jpeg(out), "image/jpeg", out.size[0], out.size[1]
        if out.mode not in ("RGB", "RGBA", "L", "LA"):
            out = out.convert("RGBA" if "transparency" in out.info or out.mode == "P" else "RGB")
        buf = io.BytesIO()
        out.save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        if len(data) > MAX_MODEL_IMAGE_BYTES:
            return _to_jpeg(out), "image/jpeg", out.size[0], out.size[1]
        return data, "image/png", out.size[0], out.size[1]


def _to_jpeg(im: Any) -> bytes:
    if im.mode != "RGB":
        im = im.convert("RGB")
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


# ---------------------------------------------------------------- extraction result

class _Extract:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.segments: list[dict] = []   # {kind, locator, text, image_bytes?}
        self.stages: dict[str, str] = {}
        self.errors: list[str] = []
        self.truncated = False
        self.images = 0
        self.rendered_pages = 0
        self.unviewable: list[str] = []
        self.extra: dict[str, Any] = {}
        self.fatal: str | None = None

    def text(self, locator: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if len(text) <= MAX_SEGMENT_CHARS:
            self.segments.append({"kind": "text", "locator": locator, "text": text})
            return
        parts = _split_long(text, MAX_SEGMENT_CHARS)
        for i, part in enumerate(parts, 1):
            self.segments.append({"kind": "text", "locator": f"{locator} (part {i}/{len(parts)})", "text": part})

    def image(self, locator: str, raw: bytes, kind: str = "image", note: str = "") -> None:
        try:
            data, mime, w, h = make_model_image(raw)
        except Exception as e:  # noqa: BLE001 - unreadable/unsupported image format
            self.unviewable.append(locator)
            self.errors.append(f"{locator}: image could not be decoded ({type(e).__name__})")
            return
        self.segments.append({"kind": kind, "locator": locator, "text": note, "image_bytes": data,
                              "image_mime": mime, "w": w, "h": h})
        if kind == "page_image":
            self.rendered_pages += 1
        else:
            self.images += 1


def _split_long(text: str, n: int) -> list[str]:
    out, buf = [], ""
    for para in re.split(r"(\n\s*\n)", text):
        if len(buf) + len(para) > n and buf:
            out.append(buf.strip())
            buf = ""
        while len(para) > n:
            out.append(para[:n])
            para = para[n:]
        buf += para
    if buf.strip():
        out.append(buf.strip())
    return out


# ---------------------------------------------------------------- type detection

def detect_kind(data: bytes, filename: str, mime: str | None) -> str | None:
    head = data[:16]
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")) \
            or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
        return "image"
    ext = Path(filename or "").suffix.lower()
    if head.startswith(b"PK\x03\x04"):
        if ext == ".docx" or (mime or "").endswith("wordprocessingml.document"):
            return "docx"
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if "word/document.xml" in z.namelist():
                    return "docx"
        except zipfile.BadZipFile:
            return None
        return None
    kind = _EXT_KIND.get(ext) or _MIME_KIND.get((mime or "").split(";")[0].strip().lower())
    if kind in ("image", "pdf", "docx"):
        return None  # extension claims a binary type but magic bytes disagree
    return kind


def _decode_text(data: bytes, ex: _Extract) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        ex.errors.append("text was not valid UTF-8; decoded as cp1252 with replacement")
        return data.decode("cp1252", errors="replace")


# ---------------------------------------------------------------- extractors

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _extract_text(data: bytes, ex: _Extract, markdown: bool) -> None:
    text = _decode_text(data, ex)
    if "\x00" in text:
        ex.fatal = "file contains NUL bytes; not a text document"
        return
    lines = text.splitlines()
    sections: list[tuple[str | None, int, int]] = []  # (heading, start_line, end_line) 1-based
    cur_head, cur_start, in_fence = None, 1, False
    for i, line in enumerate(lines, 1):
        if _FENCE.match(line):
            in_fence = not in_fence
        m = _HEADING.match(line) if (markdown and not in_fence) else None
        if m:
            if i > cur_start or cur_head is not None:
                sections.append((cur_head, cur_start, i - 1))
            cur_head, cur_start = m.group(2).strip(), i
    sections.append((cur_head, cur_start, len(lines)))
    n = 0
    for head, a, b in sections:
        body = "\n".join(lines[a - 1:b])
        if not body.strip():
            continue
        if head is not None:
            n += 1
            ex.text(f"section {n}: {head[:120]}", body)
        else:
            # un-headed text: chunk by lines so locators stay precise
            _chunk_lines(ex, lines, a, b)
    ex.stages["extract_text"] = "ok"


def _chunk_lines(ex: _Extract, lines: list[str], a: int, b: int, max_lines: int = 60) -> None:
    start, size = a, 0
    for i in range(a, b + 1):
        size += len(lines[i - 1]) + 1
        if i - start + 1 >= max_lines or size >= MAX_SEGMENT_CHARS or i == b:
            ex.text(f"lines {start}-{i}", "\n".join(lines[start - 1:i]))
            start, size = i + 1, 0


def _extract_pdf(data: bytes, ex: _Extract) -> None:
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c

    try:
        pdf = pdfium.PdfDocument(data)
    except Exception as e:  # noqa: BLE001 - untrusted input must not crash ingestion
        ex.fatal = f"PDF could not be opened ({type(e).__name__}: {str(e)[:120]})"
        return
    try:
        total = len(pdf)
        n = min(total, MAX_PDF_PAGES)
        ex.extra["pages"] = {"total": total, "processed": n}
        if total > MAX_PDF_PAGES:
            ex.truncated = True
            ex.errors.append(f"PDF has {total} pages; only the first {MAX_PDF_PAGES} were processed")
        text_ok = render_ok = True
        page_images = 0
        for i in range(n):
            locator = f"page {i + 1}"
            page = pdf[i]
            try:
                try:
                    tp = page.get_textpage()
                    txt = tp.get_text_bounded() or ""
                    tp.close()
                except Exception as e:  # noqa: BLE001 - untrusted input must not crash ingestion
                    txt = ""
                    text_ok = False
                    ex.errors.append(f"{locator}: text extraction failed ({type(e).__name__})")
                ex.text(locator, txt)
                n_img = n_path = 0
                try:
                    for obj in page.get_objects():
                        if obj.type == pdfium_c.FPDF_PAGEOBJ_IMAGE:
                            n_img += 1
                        elif obj.type == pdfium_c.FPDF_PAGEOBJ_PATH:
                            n_path += 1
                except Exception as e:  # noqa: BLE001 - untrusted input must not crash ingestion
                    ex.errors.append(f"{locator}: object scan failed ({type(e).__name__}); rendering page")
                    n_img = 1
                page_images += n_img
                if n_img or n_path >= VECTOR_PATH_THRESHOLD or len(txt.strip()) < LOW_TEXT_CHARS:
                    try:
                        w, h = page.get_size()
                        scale = min(2.5, MODEL_IMAGE_MAX_EDGE / max(w, h, 1))
                        bm = page.render(scale=scale)
                        pil = bm.to_pil()
                        buf = io.BytesIO()
                        pil.save(buf, format="PNG", optimize=True)
                        reason = "embedded images" if n_img else ("vector graphics" if n_path >= VECTOR_PATH_THRESHOLD
                                                                 else "little or no extractable text")
                        ex.image(locator, buf.getvalue(), kind="page_image",
                                 note=f"[rendered {locator}: {n_img} embedded image(s); reason: {reason}]")
                    except Exception as e:  # noqa: BLE001 - untrusted input must not crash ingestion
                        render_ok = False
                        ex.unviewable.append(locator)
                        ex.errors.append(f"{locator}: render failed ({type(e).__name__})")
            finally:
                page.close()
        ex.images += page_images  # embedded images are counted even though they're seen via page renders
        ex.extra["embedded_images_in_pages"] = page_images
        ex.stages["extract_text"] = "ok" if text_ok else "partial"
        ex.stages["render_pages"] = "ok" if render_ok else "partial"
    finally:
        pdf.close()


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_V = "urn:schemas-microsoft-com:vml"


def _embed_rids(el: Any) -> list[str]:
    out = []
    for blip in el.iter(f"{{{_A}}}blip"):
        rid = blip.get(f"{{{_R}}}embed") or blip.get(f"{{{_R}}}link")
        if rid:
            out.append(rid)
    for im in el.iter(f"{{{_V}}}imagedata"):
        rid = im.get(f"{{{_R}}}id")
        if rid:
            out.append(rid)
    return out


def _extract_docx(data: bytes, ex: _Extract) -> None:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        d = docx.Document(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001 - untrusted input must not crash ingestion
        ex.fatal = f"DOCX could not be opened ({type(e).__name__})"
        return
    part = d.part
    placed: set[str] = set()
    img_n = 0
    para_n = 0
    table_n = 0
    heading = ""

    def add_images(el: Any, where: str) -> None:
        nonlocal img_n
        for rid in _embed_rids(el):
            rel = part.rels.get(rid)
            if rel is None or rel.is_external:
                ex.errors.append(f"image reference {rid} ({where}) is external or missing; not fetched")
                continue
            tgt = rel.target_part
            placed.add(str(tgt.partname))
            img_n += 1
            loc = f"image {img_n} ({where}{', under heading: ' + heading[:80] if heading else ''})"
            ex.image(loc, tgt.blob, note=f"[embedded image {Path(str(tgt.partname)).name}]")

    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            para_n += 1
            p = Paragraph(child, d)
            text = p.text
            style = (p.style.name if p.style is not None else "") or ""
            if style.lower().startswith("heading") or style.lower() == "title":
                if text.strip():
                    heading = text.strip()
                    ex.text(f"heading: {heading[:120]}", text)
            else:
                ex.text(f"paragraph {para_n}", text)
            add_images(child, f"paragraph {para_n}")
        elif tag == "tbl":
            table_n += 1
            t = Table(child, d)
            lines = []
            for r in t.rows:
                cells = []
                for c in r.cells:
                    ct = c.text.replace("\n", " ").strip()
                    if not cells or cells[-1] != ct:  # merged cells repeat
                        cells.append(ct)
                lines.append(" | ".join(cells))
            ex.text(f"table {table_n}", "\n".join(lines))
            add_images(child, f"table {table_n}")
    # images in the package not referenced from the body (headers, footers, text boxes elsewhere)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for name in sorted(z.namelist()):
                if name.startswith("word/media/") and "/" + name not in placed:
                    img_n += 1
                    ex.image(f"image {img_n} (unplaced: {name})", z.read(name), note=f"[package image {name}]")
    except zipfile.BadZipFile:
        ex.errors.append("could not scan word/media for unplaced images")
    ex.stages["extract_text"] = "ok"
    ex.stages["extract_images"] = "ok" if not ex.unviewable else "partial"


def _extract_ipynb(data: bytes, ex: _Extract) -> None:
    try:
        nb = json.loads(_decode_text(data, ex))
    except json.JSONDecodeError as e:
        ex.fatal = f"notebook is not valid JSON ({e.msg})"
        return
    cells = nb.get("cells") if isinstance(nb, dict) else None
    if not isinstance(cells, list):
        ex.fatal = "unsupported notebook format (expected nbformat 4 'cells')"
        return

    def src(v: Any) -> str:
        return "".join(v) if isinstance(v, list) else (v if isinstance(v, str) else "")

    for i, cell in enumerate(cells, 1):
        if not isinstance(cell, dict):
            continue
        ctype = cell.get("cell_type", "unknown")
        ctype = ctype if ctype in ("markdown", "code", "raw") else "unknown"
        loc = f"cell {i} ({ctype})"
        parts = [src(cell.get("source"))]
        pending_images: list[tuple[str, bytes, str]] = []
        if ctype == "code":
            out_n = 0
            for out in cell.get("outputs") or []:
                if not isinstance(out, dict):
                    continue
                ot = out.get("output_type")
                if ot == "stream":
                    parts.append("[output]\n" + src(out.get("text")))
                elif ot == "error":
                    parts.append(f"[error] {out.get('ename', '')}: {out.get('evalue', '')}")
                elif ot in ("execute_result", "display_data"):
                    bundle = out.get("data") or {}
                    if "text/plain" in bundle:
                        parts.append("[output]\n" + src(bundle["text/plain"]))
                    for mt in ("image/png", "image/jpeg", "image/gif"):
                        if mt in bundle:
                            out_n += 1
                            try:
                                raw = base64.b64decode(src(bundle[mt]), validate=False)
                            except Exception:  # noqa: BLE001 - untrusted input must not crash ingestion
                                ex.errors.append(f"{loc} output {out_n}: invalid base64 image")
                                ex.unviewable.append(f"{loc} output {out_n}")
                                continue
                            pending_images.append((f"{loc} output {out_n} (image)", raw, f"[notebook output {mt}]"))
                            break
        ex.text(loc, "\n".join(p for p in parts if p))
        for iloc, raw, note in pending_images:
            ex.image(iloc, raw, note=note)
    ex.stages["parse_cells"] = "ok"
    ex.stages["execute"] = "never (notebook code is not executed)"


def _extract_image(data: bytes, ex: _Extract) -> None:
    before = len(ex.errors)
    ex.image("image", data, note="[uploaded image]")
    if len(ex.errors) > before and not ex.segments:
        ex.fatal = ex.errors[-1]
        return
    ex.stages["decode_image"] = "ok"


# ---------------------------------------------------------------- main entry

@contextmanager
def _maybe_tx(conn: sqlite3.Connection) -> Iterator[None]:
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _build_processing(ex: _Extract, kind: str | None) -> tuple[str, dict]:
    text_chars = sum(len(s["text"]) for s in ex.segments if s["kind"] == "text")
    visual_segments = sum(1 for s in ex.segments if s["kind"] != "text")
    needs_vision = visual_segments > 0 or bool(ex.unviewable)
    if ex.fatal:
        note = "not processed"
    elif ex.unviewable:
        note = (f"{visual_segments} visual segment(s) available for a vision model; UNAVAILABLE (could not be "
                f"decoded/rendered): {', '.join(ex.unviewable[:10])}. Text extraction is not a complete reading.")
    elif needs_vision:
        note = (f"{ex.images} image(s) and {ex.rendered_pages} rendered page(s) carry visual content that text "
                "extraction cannot capture; answer questions about them with a vision-capable model.")
    else:
        note = "no visual content detected; text extraction covers the document"
    proc = {
        "type": kind,
        "stages": ex.stages,
        "text_chars": text_chars,
        "segments": {k: sum(1 for s in ex.segments if s["kind"] == k) for k in ("text", "image", "page_image")},
        "visual": {"images": ex.images, "rendered_pages": ex.rendered_pages, "needs_vision": needs_vision,
                   "vision_note": note, "unavailable": ex.unviewable},
        "truncated": ex.truncated,
        "errors": ([ex.fatal] if ex.fatal else []) + ex.errors,
        **ex.extra,
    }
    if ex.fatal:
        status = "failed"
    elif not ex.segments:
        status = "partial"
        proc["errors"].append("no extractable content (empty document)")
    elif ex.truncated or ex.errors or ex.unviewable:
        status = "partial"
    else:
        status = "processed"
    return status, proc


def _find_document(conn: sqlite3.Connection, origin: str, origin_ref: str | None, title: str,
                   project_id: str | None) -> dict | None:
    if origin_ref:
        return row(conn, "SELECT * FROM source_documents WHERE origin=? AND origin_ref=? AND deleted_at IS NULL "
                         "ORDER BY created_at DESC LIMIT 1", (origin, origin_ref))
    return row(conn, "SELECT * FROM source_documents WHERE origin=? AND origin_ref IS NULL AND title=? "
                     "AND project_id IS ? AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 1",
               (origin, title, project_id))


def _result(doc: dict, ver: dict, deduped: bool, n_segments: int) -> dict:
    return {"document_id": doc["id"], "version_id": ver["id"], "version": ver["version"], "sha256": ver["sha256"],
            "bytes": ver["bytes"], "status": ver["status"], "processing": jload(ver["processing"], {}),
            "deduped": deduped, "title": doc["title"], "segments": n_segments}


def ingest_bytes(conn: sqlite3.Connection, data: bytes, filename: str, mime: str | None, project_id: str | None,
                 origin: str = "upload", origin_ref: str | None = None) -> dict:
    """Ingest one file. Same document identity + same hash as current version -> existing version returned."""
    title = (Path(filename or "").name or "untitled")[:255]
    sha = sha256_bytes(data)
    existing = _find_document(conn, origin, origin_ref, title, project_id)
    if existing and existing["current_version_id"]:
        cur = row(conn, "SELECT * FROM source_versions WHERE id=?", (existing["current_version_id"],))
        if cur and cur["sha256"] == sha:
            n = row(conn, "SELECT COUNT(*) n FROM evidence_segments WHERE source_version_id=?", (cur["id"],))["n"]
            return _result(existing, cur, True, n)

    kind = None
    stored_rel = None
    if len(data) > MAX_FILE_BYTES:
        ex = _Extract("oversize")
        ex.fatal = f"file is {len(data)} bytes; limit is {MAX_FILE_BYTES} bytes (20 MB)"
    else:
        kind = detect_kind(data, filename, mime)
        ex = _Extract(kind or "unknown")
        stored_rel = store_original(data, sha)
        ex.stages["store_original"] = "ok"
        if kind is None:
            ex.fatal = f"unsupported or unrecognized file type (filename extension {Path(filename or '').suffix!r}, " \
                       f"mime {mime!r})"
        else:
            try:
                {"text": lambda: _extract_text(data, ex, False),
                 "markdown": lambda: _extract_text(data, ex, True),
                 "pdf": lambda: _extract_pdf(data, ex),
                 "docx": lambda: _extract_docx(data, ex),
                 "ipynb": lambda: _extract_ipynb(data, ex),
                 "image": lambda: _extract_image(data, ex)}[kind]()
            except Exception as e:  # noqa: BLE001 - never let a hostile file crash ingestion
                ex.fatal = f"extraction error ({type(e).__name__}: {str(e)[:160]})"
    status, proc = _build_processing(ex, kind)
    if status == "failed":
        ex.segments = []

    # write derived images before the DB transaction (content-addressed, idempotent)
    for s in ex.segments:
        if "image_bytes" in s:
            s["image_path"] = _store_derived(s.pop("image_bytes"), "jpg" if s["image_mime"] == "image/jpeg" else "png")

    now = now_iso()
    with _maybe_tx(conn):
        if existing:
            doc_id = existing["id"]
            vnum = (row(conn, "SELECT MAX(version) v FROM source_versions WHERE document_id=?", (doc_id,))["v"] or 0) + 1
        else:
            doc_id = new_id("doc")
            vnum = 1
            conn.execute("INSERT INTO source_documents (id, project_id, origin, origin_ref, title, mime, created_at) "
                         "VALUES (?,?,?,?,?,?,?)",
                         (doc_id, project_id, origin, origin_ref, title,
                          mime or _KIND_MIME.get(kind or "") or (f"image/{kind}" if kind == "image" else None), now))
        ver_id = new_id("ver")
        conn.execute("INSERT INTO source_versions (id, document_id, version, sha256, bytes, stored_path, fetched_at, "
                     "status, processing) VALUES (?,?,?,?,?,?,?,?,?)",
                     (ver_id, doc_id, vnum, sha, len(data), stored_rel, now, status, jdump(proc)))
        for s in ex.segments:
            seg_id = new_id("seg")
            conn.execute("INSERT INTO evidence_segments (id, source_version_id, locator, kind, text, image_path, "
                         "image_mime, created_at) VALUES (?,?,?,?,?,?,?,?)",
                         (seg_id, ver_id, s["locator"], s["kind"], s["text"], s.get("image_path"),
                          s.get("image_mime"), now))
            if s["kind"] == "text":
                conn.execute("INSERT INTO evidence_fts (text, segment_id) VALUES (?,?)", (s["text"], seg_id))
        # a failed re-ingest must not displace a usable current version
        if status != "failed" or not (existing and existing["current_version_id"]):
            conn.execute("UPDATE source_documents SET current_version_id=? WHERE id=?", (ver_id, doc_id))
        audit(conn, "system", "knowledge.ingest", f"Ingested document version {vnum} ({status})", "source_document",
              doc_id, {"version_id": ver_id, "sha256": sha, "bytes": len(data), "status": status, "type": kind})
    doc = row(conn, "SELECT * FROM source_documents WHERE id=?", (doc_id,))
    ver = row(conn, "SELECT * FROM source_versions WHERE id=?", (ver_id,))
    return _result(doc, ver, False, len(ex.segments))


def document_status(conn: sqlite3.Connection, document_id: str) -> dict | None:
    doc = row(conn, "SELECT * FROM source_documents WHERE id=?", (document_id,))
    if not doc:
        return None
    vers = rows(conn, "SELECT id, version, sha256, bytes, status, processing, fetched_at FROM source_versions "
                      "WHERE document_id=? ORDER BY version", (document_id,))
    for v in vers:
        v["processing"] = jload(v["processing"], {})
    return {**doc, "versions": vers}


def delete_document(conn: sqlite3.Connection, document_id: str) -> dict:
    """Forget a document: remove segments + FTS rows, mark deleted, remove unreferenced stored files."""
    doc = row(conn, "SELECT * FROM source_documents WHERE id=?", (document_id,))
    if not doc:
        return {"deleted": False, "reason": "not found"}
    vers = rows(conn, "SELECT id, stored_path FROM source_versions WHERE document_id=?", (document_id,))
    ver_ids = [v["id"] for v in vers]
    segs = rows(conn, f"SELECT id, image_path FROM evidence_segments WHERE source_version_id IN "  # nosec B608 placeholders only; values bound
                      f"({','.join('?' * len(ver_ids))})", ver_ids) if ver_ids else []
    originals = {v["stored_path"] for v in vers if v["stored_path"]}
    derived = {s["image_path"] for s in segs if s["image_path"]}
    with _maybe_tx(conn):
        for s in segs:
            conn.execute("DELETE FROM evidence_fts WHERE segment_id=?", (s["id"],))
            conn.execute("DELETE FROM evidence_segments WHERE id=?", (s["id"],))
        conn.execute("UPDATE source_versions SET stored_path=NULL WHERE document_id=?", (document_id,))
        conn.execute("UPDATE source_documents SET deleted_at=? WHERE id=?", (now_iso(), document_id))
        audit(conn, "user", "knowledge.delete", "Deleted document and its evidence", "source_document", document_id,
              {"segments": len(segs), "versions": len(vers)})
    removed = 0
    for rel in originals:
        if not row(conn, "SELECT 1 x FROM source_versions WHERE stored_path=? LIMIT 1", (rel,)):
            removed += _unlink(rel)
    for rel in derived:
        if not row(conn, "SELECT 1 x FROM evidence_segments WHERE image_path=? LIMIT 1", (rel,)):
            removed += _unlink(rel)
    return {"deleted": True, "segments_removed": len(segs), "files_removed": removed}


def _unlink(rel: str) -> int:
    try:
        resolve_path(rel).unlink()
        return 1
    except FileNotFoundError:
        return 0
