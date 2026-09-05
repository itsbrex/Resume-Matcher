"""Document container and resource-boundary tests for resume uploads."""

import atexit
import asyncio
import io
import os
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_ISOLATED_ROOT = Path(tempfile.mkdtemp(prefix="resume-matcher-stage08-"))
atexit.register(shutil.rmtree, _ISOLATED_ROOT, ignore_errors=True)
os.environ["DATA_DIR"] = str(_ISOLATED_ROOT / "data")
os.environ["CONFIG_FILE_PATH"] = str(_ISOLATED_ROOT / "config.json")

import pytest
from docx import Document

from app.services.parser import parse_document


def _docx_bytes(text: str) -> bytes:
    document = Document()
    document.add_paragraph(text)
    stream = io.BytesIO()
    document.save(stream)
    return stream.getvalue()


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
