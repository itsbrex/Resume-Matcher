"""Document container and resource-boundary tests for resume uploads."""

import atexit
import asyncio
import io
import os
import shutil
import tempfile
import threading
import zipfile
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_ISOLATED_ROOT = Path(tempfile.mkdtemp(prefix="resume-matcher-stage08-"))
atexit.register(shutil.rmtree, _ISOLATED_ROOT, ignore_errors=True)
os.environ["DATA_DIR"] = str(_ISOLATED_ROOT / "data")
os.environ["CONFIG_FILE_PATH"] = str(_ISOLATED_ROOT / "config.json")

import pytest
from docx import Document

from app.services.parser import DocumentResourceLimitError, parse_document


def _docx_bytes(text: str) -> bytes:
    document = Document()
    document.add_paragraph(text)
    stream = io.BytesIO()
    document.save(stream)
    return stream.getvalue()


def _pdf_with_content_streams(
    contents: list[bytes], *, filter_name: bytes | None = None
) -> bytes:
    """Build one valid PDF whose page references the requested content streams."""
    filter_entry = b" /Filter /" + filter_name if filter_name else b""
    content_references = b" ".join(
        f"{number} 0 R".encode() for number in range(5, 5 + len(contents))
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents ["
            + content_references
            + b"] >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    objects.extend(
        (
            b"<< /Length "
            + str(len(content)).encode()
            + filter_entry
            + b" >>\nstream\n"
            + content
            + b"\nendstream"
        )
        for content in contents
    )
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{number} 0 obj\n".encode())
        pdf.extend(body)
        pdf.extend(b"\nendobj\n")
    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode()
    )
    return bytes(pdf)


def _pdf_bytes(content: bytes, *, flate: bool = False) -> bytes:
    """Build one valid PDF whose page content uses the requested stream bytes."""
    filter_name = b"FlateDecode" if flate else None
    return _pdf_with_content_streams([content], filter_name=filter_name)


def _xref_stream_pdf_bytes(*, decoded_padding: int = 0) -> bytes:
    """Build a valid PDF 1.5 file whose cross-reference table is a Flate stream."""
    page_content = b"BT /F1 12 Tf 72 720 Td (XRef engineer) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        (
            b"<< /Length "
            + str(len(page_content)).encode()
            + b" >>\nstream\n"
            + page_content
            + b"\nendstream"
        ),
    ]
    pdf = bytearray(b"%PDF-1.5\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{number} 0 obj\n".encode())
        pdf.extend(body)
        pdf.extend(b"\nendobj\n")

    xref_offset = len(pdf)
    offsets.append(xref_offset)
    entries = bytearray(b"\x00\x00\x00\x00\x00\xff\xff")
    for offset in offsets[1:]:
        entries.extend(b"\x01" + offset.to_bytes(4, "big") + b"\x00\x00")
    free_entry = b"\x00\x00\x00\x00\x00\xff\xff"
    extra_entries = (decoded_padding + len(free_entry) - 1) // len(free_entry)
    entries.extend(free_entry * extra_entries)
    object_count = len(entries) // len(free_entry)
    encoded_entries = zlib.compress(entries, level=9)
    pdf.extend(
        b"6 0 obj\n<< /Type /XRef /Size "
        + str(object_count).encode()
        + b" /W [1 4 2] /Index [0 "
        + str(object_count).encode()
        + b"] /Root 1 0 R /Filter /FlateDecode /Length "
        + str(len(encoded_entries)).encode()
        + b" >>\nstream\n"
        + encoded_entries
        + b"\nendstream\nendobj\n"
    )
    pdf.extend(f"startxref\n{xref_offset}\n%%EOF\n".encode())
    return bytes(pdf)


async def test_parse_document_rejects_malformed_pdf_container() -> None:
    """Converter fallback text must not make a corrupt PDF valid."""
    with pytest.raises(ValueError, match="valid PDF, DOC, or DOCX"):
        await parse_document(b"%PDF-1.4 broken", "broken.pdf")


async def test_parse_document_rejects_malformed_docx_container() -> None:
    """A `PK` prefix alone must not make fallback text a DOCX document."""
    with pytest.raises(ValueError, match="valid PDF, DOC, or DOCX"):
        await parse_document(b"PK corrupt", "broken.docx")


async def test_parse_document_rejects_malformed_legacy_doc_container() -> None:
    """Legacy DOC support requires a compound-file container, not fallback text."""
    with pytest.raises(ValueError, match="valid PDF, DOC, or DOCX"):
        await parse_document(b"plain text pretending to be Word", "broken.doc")


async def test_parse_document_rejects_docx_over_unpacked_limit() -> None:
    """A small compressed upload cannot expand beyond the archive budget."""
    compressed = _docx_bytes("A" * (17 * 1024 * 1024))
    assert len(compressed) < 4 * 1024 * 1024

    with pytest.raises(ValueError, match="expanded content exceeds"):
        await parse_document(compressed, "expanded.docx")


async def test_parse_document_rejects_pdf_over_decoded_stream_limit() -> None:
    """A tiny Flate stream cannot expand past the PDF processing budget."""
    expanded = b"%" + (b"A" * (17 * 1024 * 1024)) + b"\nBT (Synthetic engineer) Tj ET"
    document = _pdf_bytes(zlib.compress(expanded, level=9), flate=True)
    assert len(document) < 4 * 1024 * 1024

    with patch("app.services.parser.MarkItDown.convert") as convert:
        with pytest.raises(
            DocumentResourceLimitError, match="expanded content exceeds"
        ):
            await parse_document(document, "expanded.pdf")

    convert.assert_not_called()


async def test_parse_document_bounds_pdf_xref_stream_during_initialization() -> None:
    """An oversized xref stream is bounded before PDFDocument can inflate it."""
    document = _xref_stream_pdf_bytes(decoded_padding=17 * 1024 * 1024)
    assert len(document) < 4 * 1024 * 1024

    with patch("app.services.parser.MarkItDown.convert") as convert:
        with pytest.raises(
            DocumentResourceLimitError, match="expanded content exceeds"
        ):
            await parse_document(document, "expanded-xref.pdf")

    convert.assert_not_called()


async def test_parse_document_applies_one_budget_across_pdf_streams() -> None:
    """Separate streams cannot each consume the full document expansion limit."""
    stream = zlib.compress(b"%" + (b"A" * (9 * 1024 * 1024)), level=9)
    document = _pdf_with_content_streams(
        [stream, stream], filter_name=b"FlateDecode"
    )
    assert len(document) < 4 * 1024 * 1024

    with patch("app.services.parser.MarkItDown.convert") as convert:
        with pytest.raises(
            DocumentResourceLimitError, match="expanded content exceeds"
        ):
            await parse_document(document, "aggregate.pdf")

    convert.assert_not_called()


async def test_parse_document_bounds_run_length_pdf_expansion() -> None:
    """The PDF cap also covers expansion filters other than Flate."""
    encoded = (b"\x81A" * ((17 * 1024 * 1024) // 128)) + b"\x80"
    document = _pdf_with_content_streams(
        [encoded], filter_name=b"RunLengthDecode"
    )
    assert len(document) < 4 * 1024 * 1024

    with patch("app.services.parser.MarkItDown.convert") as convert:
        with pytest.raises(
            DocumentResourceLimitError, match="expanded content exceeds"
        ):
            await parse_document(document, "run-length.pdf")

    convert.assert_not_called()


async def test_parse_document_accepts_normal_flate_pdf() -> None:
    """The PDF resource guard preserves ordinary compressed text documents."""
    content = zlib.compress(b"BT /F1 12 Tf 72 720 Td (Flate engineer) Tj ET")

    assert "Flate engineer" in await parse_document(
        _pdf_bytes(content, flate=True), "normal.pdf"
    )


async def test_parse_document_rejects_too_many_docx_members() -> None:
    """Archive entry count is bounded independently from byte expansion."""
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("word/document.xml", "<document />")
        for index in range(1_023):
            archive.writestr(f"word/media/{index}.txt", "x")

    with pytest.raises(ValueError, match="more than 1024 entries"):
        await parse_document(stream.getvalue(), "too-many-members.docx")


async def test_parse_document_rejects_extracted_text_over_prompt_limit() -> None:
    """Extracted text is bounded before it can become an LLM prompt."""
    compressed = _docx_bytes("A" * (3 * 1024 * 1024))

    with pytest.raises(ValueError, match="extracted text exceeds"):
        await parse_document(compressed, "prompt-too-large.docx")


async def test_real_docx_conversion_keeps_event_loop_responsive() -> None:
    """A competing coroutine advances while real MarkItDown conversion runs."""
    compressed = _docx_bytes("A" * (1024 * 1024))
    ticks = 0
    running = True

    async def heartbeat() -> None:
        nonlocal ticks
        while running:
            ticks += 1
            await asyncio.sleep(0.001)

    pulse = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)
    extracted = await asyncio.wait_for(
        parse_document(compressed, "responsive.docx"), timeout=10
    )
    running = False
    await pulse

    assert len(extracted) == 1024 * 1024
    assert ticks >= 2


async def test_document_conversion_uses_at_most_two_workers() -> None:
    """A third conversion waits until one of the two worker slots is released."""
    document = _docx_bytes("bounded worker test")
    lock = threading.Lock()
    release = threading.Event()
    two_entered = threading.Event()
    active = 0
    maximum_active = 0

    def slow_convert(_converter: object, _path: str) -> SimpleNamespace:
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
            if active == 2:
                two_entered.set()
        assert release.wait(timeout=5)
        with lock:
            active -= 1
        return SimpleNamespace(text_content="bounded worker test")

    with patch("app.services.parser.MarkItDown.convert", new=slow_convert):
        tasks = [
            asyncio.create_task(parse_document(document, f"worker-{index}.docx"))
            for index in range(3)
        ]
        assert await asyncio.to_thread(two_entered.wait, 5)
        await asyncio.sleep(0.05)
        assert maximum_active == 2
        release.set()
        assert await asyncio.wait_for(asyncio.gather(*tasks), timeout=5) == [
            "bounded worker test"
        ] * 3


async def test_cancellation_waits_for_converter_tempfile_cleanup(tmp_path: Path) -> None:
    """Cancellation does not abandon the worker before its finally cleanup."""
    converter_dir = tmp_path / "converter"
    converter_dir.mkdir()
    document = _docx_bytes("cleanup test")
    entered = threading.Event()
    release = threading.Event()

    def slow_convert(_converter: object, _path: str) -> SimpleNamespace:
        entered.set()
        assert release.wait(timeout=5)
        return SimpleNamespace(text_content="cleanup test")

    with (
        patch("app.services.parser.tempfile.tempdir", str(converter_dir)),
        patch("app.services.parser.MarkItDown.convert", new=slow_convert),
    ):
        task = asyncio.create_task(parse_document(document, "cancelled.docx"))
        assert await asyncio.to_thread(entered.wait, 5)
        assert list(converter_dir.iterdir())
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert list(converter_dir.iterdir()) == []


async def test_cancellation_logs_late_converter_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A worker failure after cancellation is logged without replacing cancellation."""
    document = _docx_bytes("late failure")
    entered = threading.Event()
    release = threading.Event()

    def failing_convert(_converter: object, _path: str) -> SimpleNamespace:
        entered.set()
        assert release.wait(timeout=5)
        raise RuntimeError("controlled late converter failure")

    with (
        caplog.at_level("ERROR", logger="app.services.parser"),
        patch("app.services.parser.tempfile.tempdir", str(tmp_path)),
        patch("app.services.parser.MarkItDown.convert", new=failing_convert),
    ):
        task = asyncio.create_task(parse_document(document, "cancelled-error.docx"))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert list(tmp_path.iterdir()) == []
    assert "Document conversion failed after request cancellation" in caplog.text
    assert "controlled late converter failure" in caplog.text


async def test_tempfile_cleanup_after_success_and_converter_error(tmp_path: Path) -> None:
    """Worker tempfiles are removed on both normal and exceptional completion."""
    converter_dir = tmp_path / "converter"
    converter_dir.mkdir()
    document = _docx_bytes("cleanup paths")
    with patch("app.services.parser.tempfile.tempdir", str(converter_dir)):
        assert await parse_document(document, "success.docx") == "cleanup paths"
        assert list(converter_dir.iterdir()) == []

        with patch(
            "app.services.parser.MarkItDown.convert",
            side_effect=RuntimeError("controlled converter error"),
        ):
            with pytest.raises(RuntimeError, match="controlled converter error"):
                await parse_document(document, "error.docx")

    assert list(converter_dir.iterdir()) == []
