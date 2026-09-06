"""Real Chromium controls for retirement across concurrent exports."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from typing import Any

import pytest
from playwright.async_api import Browser, Error as PlaywrightError, Page

from app import pdf
from tests.integration.test_pdf_lifecycle import RESUME_PRINT_DATA_URL


@pytest.fixture(autouse=True)
async def clean_renderer(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    await pdf.close_pdf_renderer()
    monkeypatch.setattr(pdf, "_PDF_RENDER_TIMEOUT_SECONDS", 10.0)
    monkeypatch.setattr(pdf, "_PDF_CLEANUP_RESERVE_SECONDS", 0.3)
    yield
    await pdf.close_pdf_renderer()
    if pdf._background_owners:
        await asyncio.wait_for(asyncio.gather(*pdf._background_owners), 3)
    assert not pdf._browser_users
    assert pdf._active_renders == 0


async def _warm_browser() -> Browser:
    try:
        await pdf.init_pdf_renderer()
    except pdf.PDFRenderError as error:
        if "executable" in str(error).lower():
            pytest.skip(str(error))
        raise
    assert pdf._browser is not None
    return pdf._browser


@pytest.mark.parametrize("failed_exports", [1, 2])
async def test_page_cleanup_failure_does_not_replay_a_healthy_export(
    monkeypatch: pytest.MonkeyPatch,
    failed_exports: int,
) -> None:
    browser = await _warm_browser()
    healthy_started = asyncio.Event()
    continue_healthy = asyncio.Event()
    rendered_pages: list[Page] = []
    original_render = pdf._render_page_to_pdf
    original_close = Page.close
    original_browser_close = browser.close
    close_calls = 0
    closed_before_healthy_finished = False
    unhealthy_url = RESUME_PRINT_DATA_URL.replace("Synthetic", "Unhealthy")

    async def render(page: Page, url: str, *args: Any) -> bytes:
        if url == RESUME_PRINT_DATA_URL:
            rendered_pages.append(page)
            healthy_started.set()
            await continue_healthy.wait()
        return await original_render(page, url, *args)

    async def close(page: Page, *args: Any, **kwargs: Any) -> None:
        if "Unhealthy" in page.url:
            raise PlaywrightError("synthetic page disposal failure")
        await original_close(page, *args, **kwargs)

    async def close_browser() -> None:
        nonlocal close_calls, closed_before_healthy_finished
        close_calls += 1
        closed_before_healthy_finished = not healthy.done()
        await original_browser_close()

    monkeypatch.setattr(browser, "close", close_browser)
    monkeypatch.setattr(pdf, "_render_page_to_pdf", render)
    monkeypatch.setattr(Page, "close", close)
    healthy = asyncio.create_task(pdf.render_resume_pdf(RESUME_PRINT_DATA_URL))
    try:
        await asyncio.wait_for(healthy_started.wait(), 2)
        unhealthy_results = await asyncio.gather(*(
            pdf.render_resume_pdf(unhealthy_url) for _ in range(failed_exports)
        ))
        assert all(result.startswith(b"%PDF") for result in unhealthy_results)
        assert pdf._browser is None
        # Let the real retirement owner run while the healthy page is active.
        await asyncio.sleep(0.1)
        continue_healthy.set()
        healthy_result = await healthy
        assert healthy_result.startswith(b"%PDF")
        assert len(rendered_pages) == 1, "A different export's cleanup replayed this page"
    finally:
        continue_healthy.set()
        await asyncio.gather(healthy, return_exceptions=True)
        if pdf._background_owners:
            await asyncio.wait_for(asyncio.gather(*pdf._background_owners), 3)
    assert not browser.is_connected()
    assert close_calls == 1 and not closed_before_healthy_finished
    assert pdf._active_renders == 0


@pytest.mark.skipif(sys.platform == "win32", reason="Read-only PID probe uses POSIX signal zero")
async def test_real_driver_stop_settles_retirement_after_browser_close_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = await _warm_browser()
    driver = pdf._playwright
    assert driver is not None
    session = await browser.new_browser_cdp_session()
    processes = await session.send("SystemInfo.getProcessInfo")
    browser_pid = next(int(p["id"]) for p in processes["processInfo"] if p["type"] == "browser")
    await session.detach()
    original_close = Browser.close
    stopped = asyncio.Event()
    original_stop = driver.stop

    async def close(target: Browser, *args: Any, **kwargs: Any) -> None:
        if target is browser:
            raise PlaywrightError("synthetic close transport error")
        await original_close(target, *args, **kwargs)

    async def stop() -> None:
        await original_stop()
        stopped.set()

    monkeypatch.setattr(Browser, "close", close)
    monkeypatch.setattr(driver, "stop", stop)
    owner = pdf._retire_shared_browser(browser)
    assert owner is not None
    try:
        await asyncio.wait_for(stopped.wait(), 3)
        # Signal zero only observes this test's own launched Chromium PID.
        with pytest.raises(ProcessLookupError):
            os.kill(browser_pid, 0)
        await asyncio.sleep(0)
        assert owner.done(), "Stopped driver and reaped Chromium still retain capacity"
        assert pdf._active_renders == 0
    finally:
        if not owner.done():
            owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


async def test_normal_browser_close_and_driver_stop_disconnects() -> None:
    browser = await _warm_browser()
    driver = pdf._playwright
    assert driver is not None
    await browser.close()
    await driver.stop()
    assert not browser.is_connected()


@pytest.mark.parametrize("release_during_shutdown", [False, True])
async def test_failed_driver_stop_retains_capacity_and_shutdown_waits(
    monkeypatch: pytest.MonkeyPatch,
    release_during_shutdown: bool,
) -> None:
    browser = await _warm_browser()
    driver = pdf._playwright
    assert driver is not None
    original_stop = driver.stop
    failed = asyncio.Event()
    release_stop = asyncio.Event()
    stop_calls = 0

    async def stop() -> None:
        nonlocal stop_calls
        stop_calls += 1
        if stop_calls == 1:
            failed.set()
            raise PlaywrightError("synthetic temporary driver stop failure")
        await release_stop.wait()
        await original_stop()

    monkeypatch.setattr(driver, "stop", stop)
    monkeypatch.setattr(pdf, "_PDF_MAX_CONCURRENCY", 1)
    owner = pdf._retire_shared_browser(browser)
    assert owner is not None
    shutdown: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(failed.wait(), 2)
        await asyncio.sleep(0)
        assert pdf._active_renders == 1, "Driver teardown failure released capacity"
        with pytest.raises(pdf.PDFRenderOverloadedError):
            await pdf.render_resume_pdf(RESUME_PRINT_DATA_URL)
        shutdown = asyncio.create_task(pdf.close_pdf_renderer())
        await asyncio.sleep(0.02)
        assert not shutdown.done(), "Shutdown ignored owned retirement"
        if not release_during_shutdown:
            await asyncio.wait_for(shutdown, 2)
            assert owner in pdf._background_owners and not owner.done()
            assert pdf._active_renders == 1
        release_stop.set()
        await asyncio.wait_for(shutdown, 2)
        await asyncio.wait_for(asyncio.shield(owner), 2)
        assert pdf._active_renders == 0
    finally:
        release_stop.set()
        await original_stop()
        if not owner.done():
            owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
        if shutdown is not None:
            await asyncio.gather(shutdown, return_exceptions=True)
