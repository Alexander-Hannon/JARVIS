from __future__ import annotations

import io

import numpy as np
from PIL import Image

from jarvis.config import get_settings
from jarvis.core.db import row, rows
from jarvis.knowledge import ingest
from jarvis.knowledge.ingest import delete_document, ingest_bytes


def segs(conn, version_id):
    return rows(conn, "SELECT * FROM evidence_segments WHERE source_version_id=? ORDER BY rowid", (version_id,))


def test_original_stored_content_addressed(conn, gen):
    data = gen.timetable_png()
    r = ingest_bytes(conn, "usr_owner", data, "timetable.png", "image/png", None)
    sha = r["sha256"]
    p = get_settings().files_dir / sha[:2] / sha
    assert p.read_bytes() == data
    v = row(conn, "SELECT * FROM source_versions WHERE id=?", (r["version_id"],))
    assert v["bytes"] == len(data) and v["version"] == 1 and v["stored_path"] == f"{sha[:2]}/{sha}"


def test_dedupe_and_versions(conn, gen):
    r1 = ingest_bytes(conn, "usr_owner", b"# A\nfirst", "notes.md", None, None, origin="local", origin_ref="notes/notes.md")
    r2 = ingest_bytes(conn, "usr_owner", b"# A\nfirst", "notes.md", None, None, origin="local", origin_ref="notes/notes.md")
    assert r2["deduped"] and r2["version_id"] == r1["version_id"]
    assert row(conn, "SELECT COUNT(*) n FROM source_versions")["n"] == 1
    r3 = ingest_bytes(conn, "usr_owner", b"# A\nsecond", "notes.md", None, None, origin="local", origin_ref="notes/notes.md")
    assert r3["document_id"] == r1["document_id"] and r3["version"] == 2 and not r3["deduped"]
    doc = row(conn, "SELECT * FROM source_documents WHERE id=?", (r1["document_id"],))
    assert doc["current_version_id"] == r3["version_id"]
    # same title without origin_ref (upload) dedupes by title as well
    u1 = ingest_bytes(conn, "usr_owner", b"hello", "a.txt", None, None)
    u2 = ingest_bytes(conn, "usr_owner", b"hello", "a.txt", None, None)
    assert u2["deduped"] and u1["version_id"] == u2["version_id"]


def test_markdown_locators_and_no_vision(conn, gen):
    r = ingest_bytes(conn, "usr_owner", gen.markdown_doc(), "notes.md", None, None)
    locs = [s["locator"] for s in segs(conn, r["version_id"])]
    assert locs == ["lines 1-2", "section 1: Goals", "section 2: Deadlines", "section 3: Notes"]
    p = r["processing"]
    assert r["status"] == "processed"
    assert p["visual"]["needs_vision"] is False and p["text_chars"] > 0
    assert set(p) >= {"stages", "text_chars", "visual", "truncated", "errors"}


def test_plain_text_line_locators(conn):
    r = ingest_bytes(conn, "usr_owner", "\n".join(f"line {i}" for i in range(1, 131)).encode(), "log.txt", None, None)
    locs = [s["locator"] for s in segs(conn, r["version_id"])]
    assert locs == ["lines 1-60", "lines 61-120", "lines 121-130"]


def test_image_only_png_needs_vision_not_empty_success(conn, gen):
    r = ingest_bytes(conn, "usr_owner", gen.timetable_png(), "timetable.png", "image/png", None)
    p = r["processing"]
    assert r["status"] == "processed"
    assert p["text_chars"] == 0
    assert p["visual"]["needs_vision"] is True and p["visual"]["images"] == 1
    s = segs(conn, r["version_id"])
    assert [(x["kind"], x["locator"]) for x in s] == [("image", "image")]


def test_large_image_downscaled_original_kept(conn, gen):
    data = gen.diagram_png()
    r = ingest_bytes(conn, "usr_owner", data, "diagram.png", None, None)
    s = segs(conn, r["version_id"])[0]
    with Image.open(ingest.resolve_path(s["image_path"])) as im:
        assert max(im.size) == 1568
    orig = get_settings().files_dir / r["sha256"][:2] / r["sha256"]
    with Image.open(orig) as im:
        assert im.size == (2400, 1200)


def test_pdf_text_page_and_rendered_image_page(conn, gen):
    r = ingest_bytes(conn, "usr_owner", gen.two_page_pdf(), "syllabus.pdf", "application/pdf", None)
    p = r["processing"]
    s = segs(conn, r["version_id"])
    kinds = [(x["kind"], x["locator"]) for x in s]
    assert ("text", "page 1") in kinds
    assert ("page_image", "page 2") in kinds
    assert ("page_image", "page 1") not in kinds  # page 1 is text-rich with no images
    assert p["visual"]["needs_vision"] is True and p["visual"]["rendered_pages"] == 1
    assert p["pages"] == {"total": 2, "processed": 2}
    page2 = next(x for x in s if x["kind"] == "page_image")
    with Image.open(ingest.resolve_path(page2["image_path"])) as im:
        assert im.format == "PNG" and max(im.size) <= 1568
        # the orange timetable cell survives rendering
        a = np.asarray(im.convert("RGB")).astype(int)
        assert ((a[..., 0] > 200) & (a[..., 1] > 100) & (a[..., 1] < 180) & (a[..., 2] < 60)).any()


def test_pdf_truncation_reported(conn, gen):
    r = ingest_bytes(conn, "usr_owner", gen.many_page_pdf(55), "scan.pdf", None, None)
    p = r["processing"]
    assert r["status"] == "partial" and p["truncated"] is True
    assert p["pages"] == {"total": 55, "processed": 50}
    assert any("50" in e for e in p["errors"])


def test_docx_embedded_image_with_position(conn, gen):
    r = ingest_bytes(conn, "usr_owner", gen.sample_docx(), "logistics.docx", None, None)
    s = segs(conn, r["version_id"])
    locs = [(x["kind"], x["locator"]) for x in s]
    assert ("text", "heading: Course Logistics") in locs
    assert ("text", "paragraph 2") in locs
    assert ("image", "image 1 (paragraph 5, under heading: Schedule)") in locs
    table = next(x for x in s if x["locator"] == "table 1")
    assert "Problem Set 3 | Friday" in table["text"]
    # image appears between the paragraph before it and the table after it
    order = [x["locator"] for x in s]
    assert order.index("paragraph 4") < order.index("image 1 (paragraph 5, under heading: Schedule)") < order.index("table 1")
    assert r["processing"]["visual"]["needs_vision"] is True
    img = next(x for x in s if x["kind"] == "image")
    with Image.open(ingest.resolve_path(img["image_path"])) as im:
        assert im.size == (600, 400)


def test_ipynb_cells_outputs_and_no_execution(conn, gen, tmp_path):
    r = ingest_bytes(conn, "usr_owner", gen.sample_ipynb(), "analysis.ipynb", None, None)
    s = segs(conn, r["version_id"])
    locs = [(x["kind"], x["locator"]) for x in s]
    assert locs == [("text", "cell 1 (markdown)"), ("text", "cell 2 (code)"),
                    ("image", "cell 2 (code) output 1 (image)")]
    code = s[1]["text"]
    assert "os.system" in code and "hello residuals" in code
    assert r["processing"]["stages"]["execute"].startswith("never")
    import os
    assert not os.path.exists("/tmp/jarvis_pwned")


def test_unknown_type_fails_with_reason(conn):
    r = ingest_bytes(conn, "usr_owner", b"\x00\x01binary", "blob.bin", "application/octet-stream", None)
    assert r["status"] == "failed"
    assert "unsupported" in r["processing"]["errors"][0]
    # magic-byte mismatch: claims PNG but isn't
    r2 = ingest_bytes(conn, "usr_owner", b"not really a png", "fake.png", "image/png", None)
    assert r2["status"] == "failed"


def test_oversize_rejected(conn, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_FILE_BYTES", 10)
    r = ingest_bytes(conn, "usr_owner", b"x" * 11, "big.txt", None, None)
    assert r["status"] == "failed" and "limit" in r["processing"]["errors"][0]


def test_injection_text_is_only_data(conn, gen):
    before = {t: row(conn, f"SELECT COUNT(*) n FROM {t}")["n"] for t in ("proposals", "approvals", "jobs")
              if row(conn, "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,))}
    r = ingest_bytes(conn, "usr_owner", gen.injection_txt(), "notes.txt", None, None)
    from jarvis.knowledge.search import search
    hits = search(conn, "usr_owner", "ignore approval create event")
    assert hits and hits[0]["document_id"] == r["document_id"]
    assert "approval" in hits[0]["excerpt"].lower() and hits[0]["untrusted"] is True
    after = {t: row(conn, f"SELECT COUNT(*) n FROM {t}")["n"] for t in before}
    assert before == after


def test_delete_removes_segments_fts_and_file(conn, gen):
    r = ingest_bytes(conn, "usr_owner", gen.sample_docx(), "logistics.docx", None, None)
    seg_ids = [s["id"] for s in segs(conn, r["version_id"])]
    img_paths = [s["image_path"] for s in segs(conn, r["version_id"]) if s["image_path"]]
    assert row(conn, "SELECT COUNT(*) n FROM evidence_fts WHERE evidence_fts MATCH 'logistics'")["n"] >= 1
    out = delete_document(conn, "usr_owner", r["document_id"])
    assert out["deleted"]
    q = f"SELECT COUNT(*) n FROM evidence_fts WHERE segment_id IN ({','.join('?' * len(seg_ids))})"
    assert row(conn, q, seg_ids)["n"] == 0
    assert row(conn, "SELECT COUNT(*) n FROM evidence_segments WHERE source_version_id=?", (r["version_id"],))["n"] == 0
    assert row(conn, "SELECT deleted_at FROM source_documents WHERE id=?", (r["document_id"],))["deleted_at"]
    assert not (get_settings().files_dir / r["sha256"][:2] / r["sha256"]).exists()
    assert all(not ingest.resolve_path(p).exists() for p in img_paths)


def test_delete_keeps_file_shared_with_other_document(conn, gen):
    data = gen.timetable_png()
    a = ingest_bytes(conn, "usr_owner", data, "a.png", None, None)
    b = ingest_bytes(conn, "usr_owner", data, "b.png", None, None)
    assert a["document_id"] != b["document_id"]
    delete_document(conn, "usr_owner", a["document_id"])
    assert (get_settings().files_dir / a["sha256"][:2] / a["sha256"]).exists()
    img = segs(conn, b["version_id"])[0]["image_path"]
    assert ingest.resolve_path(img).exists()


def test_corrupt_pdf_fails_cleanly(conn):
    r = ingest_bytes(conn, "usr_owner", b"%PDF-1.4\ngarbage", "bad.pdf", None, None)
    assert r["status"] == "failed" and r["processing"]["errors"]


def test_empty_text_not_success(conn):
    r = ingest_bytes(conn, "usr_owner", b"   \n", "empty.txt", None, None)
    assert r["status"] == "partial"
    assert any("no extractable content" in e for e in r["processing"]["errors"])


def test_image_bytes_roundtrip_via_pillow(conn):
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), "red").save(buf, format="JPEG")
    r = ingest_bytes(conn, "usr_owner", buf.getvalue(), "red.jpg", None, None)
    s = segs(conn, r["version_id"])[0]
    assert s["image_mime"] == "image/jpeg"
