"""PDF rendering utilities using headless Chromium."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Awaitable, NoReturn, Optional, TypeVar

from playwright.async_api import (
    Browser,
    Error as PlaywrightError,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# Explicit, bounded navigation/selector timeout. Chosen over Playwright's
# implicit 30s default so a slow-but-working render (large resume, cold cache,
# modest hardware) still completes, while a genuinely stuck page still fails
# in finite time rather than hanging.
_NAV_TIMEOUT_MS = 60_000


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """Read an integer operational limit without allowing unsafe extremes."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid %s; using default %d", name, default)
        return default
    if not minimum <= value <= maximum:
        logger.warning(
            "%s outside supported range [%d, %d]; using default %d",
            name,
            minimum,
            maximum,
            default,
        )
        return default
    return value


def _bounded_env_float(
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    """Read a finite floating-point operational limit within fixed bounds."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid %s; using default %.1f", name, default)
        return default
    if not minimum <= value <= maximum:
        logger.warning(
            "%s outside supported range [%.1f, %.1f]; using default %.1f",
            name,
            minimum,
            maximum,
            default,
        )
        return default
    return value


# Real Chromium measurement on a representative synthetic resume showed most
# throughput at four pages (18.73/s) with little gain at eight (20.14/s).
_PDF_MAX_CONCURRENCY = _bounded_env_int("PDF_MAX_CONCURRENCY", 4, 1, 16)
_PDF_RENDER_TIMEOUT_SECONDS = _bounded_env_float(
    "PDF_RENDER_TIMEOUT_SECONDS", 75.0, 1.0, 600.0
)
_PDF_CLEANUP_RESERVE_SECONDS = _bounded_env_float(
    "PDF_CLEANUP_RESERVE_SECONDS", 5.0, 0.1, 30.0
)


class PDFRenderError(Exception):
    """Custom exception for PDF rendering errors with helpful messages."""

    pass


class PDFRenderOverloadedError(PDFRenderError):
    """Raised when all renderer slots are active; exports are never queued."""


class PDFRenderTimeoutError(PDFRenderError):
    """Raised when the shared export lifecycle deadline expires."""


class _PDFDeadlineExceeded(TimeoutError):
    """Internal timeout carrying the stage that exhausted the shared budget."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__(stage)


_playwright: Playwright | None = None
_browser: Optional[Browser] = None
_init_lock = asyncio.Lock()  # Lock to prevent race condition during initialization
_subprocess_lock = threading.Lock()
_subprocess_supported = True
_admission_lock = threading.Lock()
_active_renders = 0
_background_owners: set[asyncio.Task[None]] = set()


def _browser_is_connected(browser: Browser | None) -> bool:
    """Return browser health without allowing a stale object to look usable."""
    if browser is None:
        return False
    try:
        return browser.is_connected()
    except Exception:
        logger.exception("Failed to inspect cached PDF browser connection state")
        return False


def _render_deadlines() -> tuple[float, float]:
    """Return absolute work and total deadlines with cleanup time reserved."""
    loop = asyncio.get_running_loop()
    total_deadline = loop.time() + _PDF_RENDER_TIMEOUT_SECONDS
    cleanup_reserve = min(
        _PDF_CLEANUP_RESERVE_SECONDS,
        _PDF_RENDER_TIMEOUT_SECONDS / 2,
    )
    return total_deadline - cleanup_reserve, total_deadline


async def _await_before_deadline(
    awaitable: Awaitable[_T],
    deadline: float,
    stage: str,
) -> _T:
    """Await one stage against an absolute shared deadline."""
    remaining = deadline - asyncio.get_running_loop().time()
    try:
        return await asyncio.wait_for(awaitable, timeout=max(0.0, remaining))
    except (TimeoutError, PlaywrightTimeoutError) as error:
        raise _PDFDeadlineExceeded(stage) from error


def _stage_timeout_ms(deadline: float) -> int:
    """Return Playwright's per-call timeout clipped to the shared deadline."""
    remaining_ms = int(
        max(0.0, deadline - asyncio.get_running_loop().time()) * 1000
    )
    return max(1, min(_NAV_TIMEOUT_MS, remaining_ms))


async def _close_before_deadline(
    awaitable: Awaitable[Any],
    deadline: float,
    resource: str,
    *,
    strict_timeout: bool = False,
) -> None:
    """Close one renderer resource within the cleanup budget."""
    try:
        await _await_before_deadline(awaitable, deadline, f"{resource} cleanup")
    except _PDFDeadlineExceeded:
        if strict_timeout:
            raise
        logger.exception("Failed to close %s within PDF cleanup budget", resource)
    except PlaywrightError:
        logger.exception("Failed to close %s within PDF cleanup budget", resource)


async def _replace_disconnected_browser(
    work_deadline: float,
    total_deadline: float,
) -> Browser:
    """Return one connected shared browser, replacing stale ownership once."""
    global _playwright, _browser

    if _browser_is_connected(_browser):
        assert _browser is not None
        return _browser

    await _await_before_deadline(
        _init_lock.acquire(), work_deadline, "browser ownership"
    )
    try:
        if _browser_is_connected(_browser):
            assert _browser is not None
            return _browser

        stale_browser = _browser
        stale_playwright = _playwright
        _browser = None
        _playwright = None

        if stale_browser is not None:
            await _close_before_deadline(
                stale_browser.close(), work_deadline, "disconnected browser"
            )
        if stale_playwright is not None:
            await _close_before_deadline(
                stale_playwright.stop(), work_deadline, "stale Playwright"
            )

        new_playwright = await _await_before_deadline(
            async_playwright().start(), work_deadline, "Playwright startup"
        )
        try:
            new_browser = await _await_before_deadline(
                _launch_browser(new_playwright), work_deadline, "browser launch"
            )
        except BaseException:
            await _close_before_deadline(
                new_playwright.stop(), total_deadline, "failed Playwright startup"
            )
            raise

        _playwright = new_playwright
        _browser = new_browser
        return new_browser
    finally:
        _init_lock.release()


async def init_pdf_renderer() -> None:
    """Initialize or replace the shared Playwright browser within one budget."""
    work_deadline, total_deadline = _render_deadlines()
    await _replace_disconnected_browser(work_deadline, total_deadline)


def _resolve_pdf_format(page_size: str) -> str:
    format_map = {
        "A4": "A4",
        "LETTER": "Letter",
    }
    return format_map.get(page_size, "A4")


def _resolve_pdf_margins(margins: Optional[dict]) -> dict:
    if margins:
        return {
            "top": f"{margins.get('top', 10)}mm",
            "right": f"{margins.get('right', 10)}mm",
            "bottom": f"{margins.get('bottom', 10)}mm",
            "left": f"{margins.get('left', 10)}mm",
        }
    return {"top": "10mm", "right": "10mm", "bottom": "10mm", "left": "10mm"}


def _find_chromium_executable() -> Optional[str]:
    """Find system Chrome/Chromium/Edge executable across platforms."""
    if sys.platform == "win32":
        candidates = [
            Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
            / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"))
            / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
            / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"))
            / "Microsoft/Edge/Application/msedge.exe",
        ]
    elif sys.platform == "darwin":
        # macOS application paths
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ]
    else:
        # Linux paths: standard locations, Snap, and Flatpak
        candidates = [
            Path("/usr/bin/google-chrome"),
            Path("/usr/bin/google-chrome-stable"),
            Path("/usr/bin/chromium"),
            Path("/usr/bin/chromium-browser"),
            Path("/usr/bin/microsoft-edge"),
            Path("/snap/bin/chromium"),
            Path("/var/lib/flatpak/exports/bin/com.google.Chrome"),
            Path("/var/lib/flatpak/exports/bin/org.chromium.Chromium"),
            Path(os.path.expanduser("~/.local/share/flatpak/exports/bin/com.google.Chrome")),
            Path(os.path.expanduser("~/.local/share/flatpak/exports/bin/org.chromium.Chromium")),
        ]

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


async def _launch_browser(playwright: Playwright) -> Browser:
    try:
        return await playwright.chromium.launch()
    except PlaywrightError as e:
        if "Executable doesn't exist" not in str(e):
            raise
        fallback_executable = _find_chromium_executable()
        if not fallback_executable:
            raise PDFRenderError(
                "Playwright browser executable is missing, and no system Chrome/Edge "
                "installation was found. Install Playwright browsers or install Chrome/Edge."
            ) from e
        return await playwright.chromium.launch(executable_path=fallback_executable)


async def _render_page_to_pdf(
    page: Page,
    url: str,
    selector: str,
    pdf_format: str,
    pdf_margins: dict,
    work_deadline: float | None = None,
) -> bytes:
    # NOTE: do NOT use wait_until="networkidle" here. The Next.js dev server
    # (HMR/Turbopack + RSC streaming) keeps the network busy, so "idle" may
    # never arrive and goto silently hangs until timeout → 503 (issues
    # #799/#808), with the failure depending on environment/network noise.
    # Wait on the real readiness condition instead — document "load", the
    # resume content selector, and fonts — all bounded by an explicit timeout
    # so the outcome is deterministic.
    if work_deadline is None:
        work_deadline = (
            asyncio.get_running_loop().time() + (_NAV_TIMEOUT_MS / 1000)
        )

    await _await_before_deadline(
        page.goto(
            url,
            wait_until="load",
            timeout=_stage_timeout_ms(work_deadline),
        ),
        work_deadline,
        "navigation",
    )
    await _await_before_deadline(
        page.wait_for_selector(
            selector,
            timeout=_stage_timeout_ms(work_deadline),
        ),
        work_deadline,
        "resume readiness",
    )
    # Bound the fonts wait too — plain page.evaluate has no timeout, so a stuck
    # font load could otherwise hang the render past _NAV_TIMEOUT_MS.
    await _await_before_deadline(
        page.wait_for_function(
            "() => document.fonts.ready.then(() => true)",
            timeout=_stage_timeout_ms(work_deadline),
        ),
        work_deadline,
        "font readiness",
    )
    return await _await_before_deadline(
        page.pdf(
            format=pdf_format,
            print_background=True,
            margin=pdf_margins,
        ),
        work_deadline,
        "PDF generation",
    )


async def _render_with_browser(
    browser: Browser,
    url: str,
    selector: str,
    pdf_format: str,
    pdf_margins: dict,
    work_deadline: float | None = None,
    total_deadline: float | None = None,
) -> bytes:
    if work_deadline is None or total_deadline is None:
        work_deadline, total_deadline = _render_deadlines()

    page: Page = await _await_before_deadline(
        browser.new_page(), work_deadline, "page creation"
    )
    render_error: BaseException | None = None
    try:
        return await _render_page_to_pdf(
            page,
            url,
            selector,
            pdf_format,
            pdf_margins,
            work_deadline,
        )
    except BaseException as error:
        render_error = error
        raise
    finally:
        cleanup_deadline = min(
            total_deadline,
            asyncio.get_running_loop().time() + _PDF_CLEANUP_RESERVE_SECONDS,
        )
        try:
            await _close_before_deadline(
                page.close(),
                cleanup_deadline,
                "PDF page",
                strict_timeout=True,
            )
        except _PDFDeadlineExceeded:
            if render_error is None:
                raise
            logger.exception(
                "PDF page cleanup exceeded its reserve after render failure"
            )


def _run_in_new_loop(coro: Awaitable[bytes]) -> bytes:
    if sys.platform == "win32":
        from asyncio.windows_events import ProactorEventLoop

        loop = ProactorEventLoop()
    else:
        loop = asyncio.new_event_loop()

    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()
            asyncio.set_event_loop(None)


def _render_resume_pdf_sync(
    url: str,
    selector: str,
    pdf_format: str,
    pdf_margins: dict,
    monotonic_deadline: float,
) -> bytes:
    async def _run() -> bytes:
        remaining = max(0.0, monotonic_deadline - time.monotonic())
        total_deadline = asyncio.get_running_loop().time() + remaining
        cleanup_reserve = min(_PDF_CLEANUP_RESERVE_SECONDS, remaining / 2)
        work_deadline = total_deadline - cleanup_reserve
        playwright = await _await_before_deadline(
            async_playwright().start(), work_deadline, "threaded Playwright startup"
        )
        browser: Browser | None = None
        try:
            browser = await _await_before_deadline(
                _launch_browser(playwright), work_deadline, "threaded browser launch"
            )
            return await _render_with_browser(
                browser,
                url,
                selector,
                pdf_format,
                pdf_margins,
                work_deadline,
                total_deadline,
            )
        finally:
            if browser is not None:
                await _close_before_deadline(
                    browser.close(), total_deadline, "threaded browser"
                )
            await _close_before_deadline(
                playwright.stop(), total_deadline, "threaded Playwright"
            )

    return _run_in_new_loop(_run())


async def _render_resume_pdf_in_thread(
    url: str,
    selector: str,
    pdf_format: str,
    pdf_margins: dict,
) -> asyncio.Task[bytes]:
    """Start a fallback worker whose lifetime can outlast its awaiting request."""
    monotonic_deadline = time.monotonic() + _PDF_RENDER_TIMEOUT_SECONDS
    return asyncio.create_task(
        asyncio.to_thread(
            _render_resume_pdf_sync,
            url,
            selector,
            pdf_format,
            pdf_margins,
            monotonic_deadline,
        )
    )


def _raise_playwright_error(error: PlaywrightError, url: str) -> NoReturn:
    error_msg = str(error)
    if "Executable doesn't exist" in error_msg:
        exe = sys.executable.replace("\\", "/")
        command = f"{exe} -m playwright install chromium"
        raise PDFRenderError(
            "Playwright browser executable is missing or out of date. "
            "Command shown for reference; quote the path if it contains spaces: "
            f"{command}"
        ) from error
    if "net::ERR_CONNECTION_REFUSED" in error_msg:
        raise PDFRenderError(
            f"Cannot connect to frontend for PDF generation. "
            f"Attempted URL: {url}. "
            f"Please ensure: 1) The frontend is running, "
            f"2) The FRONTEND_BASE_URL environment variable in the backend .env file "
            f"matches the URL where your frontend is accessible."
        ) from error
    # Catch-all: the raw Playwright message can carry internal navigation URLs
    # and a full call log. Log it server-side; return a generic message to the
    # client (CLAUDE.md rule 5 — and it stops the verbose trace from overflowing
    # the client error modal, #811).
    logger.error("PDF rendering failed for %s: %s", url, error_msg)
    raise PDFRenderError(
        "PDF rendering failed. Please try again, or try a simpler resume or a "
        "different template."
    ) from error


def _loop_supports_subprocess() -> bool:
    if sys.platform != "win32":
        return True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return True
    return loop.__class__.__name__ == "ProactorEventLoop"


async def close_pdf_renderer() -> None:
    """Close the Playwright browser instance."""
    global _playwright, _browser
    await _init_lock.acquire()
    try:
        browser = _browser
        playwright = _playwright
        _browser = None
        _playwright = None
        if browser is not None:
            try:
                await browser.close()
            except PlaywrightError:
                logger.exception("Failed to close PDF browser during shutdown")
        if playwright is not None:
            try:
                await playwright.stop()
            except PlaywrightError:
                logger.exception("Failed to stop Playwright during shutdown")
    finally:
        _init_lock.release()


def _acquire_render_slot() -> None:
    """Acquire fail-fast renderer capacity without creating an implicit queue."""
    global _active_renders
    with _admission_lock:
        if _active_renders >= _PDF_MAX_CONCURRENCY:
            raise PDFRenderOverloadedError(
                "PDF renderer is busy. Please try again shortly."
            )
        _active_renders += 1


def _release_render_slot() -> None:
    """Release one renderer slot, including slots owned by fallback workers."""
    global _active_renders
    with _admission_lock:
        if _active_renders <= 0:
            logger.error("PDF renderer admission counter underflow")
            return
        _active_renders -= 1


async def _release_slot_after_worker(worker: asyncio.Task[bytes]) -> None:
    """Retain capacity until a non-cancellable thread worker actually exits."""
    try:
        await worker
    except BaseException:
        pass
    finally:
        _release_render_slot()


def _transfer_slot_to_worker(worker: asyncio.Task[bytes]) -> None:
    """Give admission ownership to a background task until ``worker`` exits."""
    owner = asyncio.create_task(_release_slot_after_worker(worker))
    _background_owners.add(owner)
    owner.add_done_callback(_background_owners.discard)


async def _render_on_shared_browser(
    url: str,
    selector: str,
    pdf_format: str,
    pdf_margins: dict,
    work_deadline: float,
    total_deadline: float,
) -> bytes:
    """Render once, retrying only when the owned browser disconnected."""
    browser = await _replace_disconnected_browser(work_deadline, total_deadline)
    for attempt in range(2):
        try:
            return await _render_with_browser(
                browser,
                url,
                selector,
                pdf_format,
                pdf_margins,
                work_deadline,
                total_deadline,
            )
        except PlaywrightError:
            if attempt == 0 and not _browser_is_connected(browser):
                browser = await _replace_disconnected_browser(
                    work_deadline,
                    total_deadline,
                )
                continue
            raise
    raise PDFRenderError("PDF renderer failed to recover.")


async def render_resume_pdf(
    url: str,
    page_size: str = "A4",
    selector: str = ".resume-print",
    margins: Optional[dict] = None,
) -> bytes:
    """Render a URL to PDF bytes.

    Args:
        url: The URL to render (print route)
        page_size: Page size format - "A4" or "LETTER"
        selector: CSS selector to wait for before rendering (default: ".resume-print")
        margins: Page margins dict with top/right/bottom/left in mm (applied to every page)

    Note:
        Margins are applied via Playwright's PDF margins, ensuring they appear
        on every page (not just the first page like HTML padding would).
    """
    global _subprocess_supported

    pdf_format = _resolve_pdf_format(page_size)
    pdf_margins = _resolve_pdf_margins(margins)
    _acquire_render_slot()
    release_slot = True
    work_deadline, total_deadline = _render_deadlines()
    try:
        subprocess_supported = True
        if not _browser_is_connected(_browser):
            with _subprocess_lock:
                if _subprocess_supported and not _loop_supports_subprocess():
                    _subprocess_supported = False
                subprocess_supported = _subprocess_supported

        if _browser_is_connected(_browser) or subprocess_supported:
            try:
                return await _render_on_shared_browser(
                    url,
                    selector,
                    pdf_format,
                    pdf_margins,
                    work_deadline,
                    total_deadline,
                )
            except NotImplementedError:
                with _subprocess_lock:
                    _subprocess_supported = False
                subprocess_supported = False

        if not subprocess_supported:
            worker = await _render_resume_pdf_in_thread(
                url, selector, pdf_format, pdf_margins
            )
            try:
                return await _await_before_deadline(
                    asyncio.shield(worker), total_deadline, "threaded PDF render"
                )
            except asyncio.CancelledError:
                _transfer_slot_to_worker(worker)
                release_slot = False
                raise
            except _PDFDeadlineExceeded:
                if not worker.done():
                    _transfer_slot_to_worker(worker)
                    release_slot = False
                raise

        raise PDFRenderError("PDF renderer failed to initialize.")
    except _PDFDeadlineExceeded as error:
        logger.warning("PDF rendering timed out during %s for %s", error.stage, url)
        raise PDFRenderTimeoutError(
            "PDF rendering timed out. Please try again, or try a simpler resume."
        ) from error
    except PlaywrightError as error:
        _raise_playwright_error(error, url)
    finally:
        if release_slot:
            _release_render_slot()
