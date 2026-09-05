"""Representative long multilingual text survives actual Chromium pagination."""

import io
from pathlib import Path

import pytest
from pdfminer.high_level import extract_text
from pdfminer.pdfpage import PDFPage
from playwright.async_api import async_playwright

from app.pdf import _launch_browser


@pytest.mark.parametrize("page_format,width_mm", [("A4", 210.0), ("Letter", 215.9)])
async def test_long_multilingual_resume_preserves_text_and_column_bounds(
    tmp_path: Path, page_format: str, width_mm: float
) -> None:
    style_root = (
        Path(__file__).resolve().parents[4] / "apps/frontend/components/resume/styles"
    )
    styles = "\n".join(
        line
        for name in ("_tokens.css", "_base.module.css", "swiss-two-column.module.css")
        for line in (style_root / name).read_text().splitlines()
        if not line.startswith("@import")
    )
    printable_width = (width_mm - 30) * 96 / 25.4
    multilingual = (
        "Développement fiable — Información profesional — 履歴書の経験 — 简历技能"
    )
    entries = "".join(
        f"<p data-text>Entry {index}: {multilingual}. Built useful systems with clear documentation.</p>"
        for index in range(1, 81)
    )
    async with async_playwright() as playwright:
        try:
            browser = await _launch_browser(playwright)
        except Exception as error:
            if "executable" in str(error).lower() or "installation was found" in str(
                error
            ):
                pytest.skip(f"Chromium unavailable: {error}")
            raise
        try:
            page = await browser.new_page()
            await page.set_content(
                f"""
                <style>
                * {{box-sizing: border-box;}}
                html, body {{margin: 0; width: {printable_width}px;}}
                {styles}
                .resume-print, .resume-body {{width: {printable_width}px; --section-gap: 16px; --margin-top:0; --margin-right:0; --margin-bottom:0; --margin-left:0;}}
                p {{font-size: 11pt; line-height: 1.4;}}
                </style>
                <main class="resume-print resume-body">
                <h1>Multilingual Synthetic Candidate</h1>
                <div class="grid"><section class="mainColumn">{entries}<p>END-OF-RESUME</p></section>
                <aside class="sidebarColumn"><p data-text>Python — SQL — 日本語 — 中文</p><p data-text>github.com/synthetic-candidate/long-multilingual-resume</p></aside></div>
                </main>
            """,
                wait_until="load",
            )
            await page.emulate_media(media="print")
            assert await page.evaluate(
                """() => Array.from(document.querySelectorAll('[data-text]')).every(node => node.scrollWidth <= node.clientWidth + 1)"""
            )
            assert await page.evaluate(
                """() => {const grid=document.querySelector('.grid'); return grid.scrollWidth <= grid.clientWidth + 1;}"""
            )
            pdf = await page.pdf(
                format=page_format,
                print_background=True,
                margin={side: "15mm" for side in ("top", "right", "bottom", "left")},
            )
            (tmp_path / f"multilingual-{page_format}.pdf").write_bytes(pdf)
            assert len(list(PDFPage.get_pages(io.BytesIO(pdf)))) >= 2
            extracted = extract_text(io.BytesIO(pdf))
            compact = "".join(extracted.split())
            for text in (
                "Développement",
                "Información",
                "履歴書の経験",
                "简历技能",
                "Entry80:",
                "END-OF-RESUME",
            ):
                assert text in compact
        finally:
            await browser.close()
