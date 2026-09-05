"""Elapsed stage records retained on success, error and cancellation."""

from __future__ import annotations

from time import monotonic
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any


@contextmanager
def measured_step(steps: list[dict[str, Any]], stage: str) -> Iterator[dict[str, Any]]:
    record: dict[str, Any] = {"stage": stage, "ok": False}
    started = monotonic()
    try:
        yield record
        record["ok"] = True
    except BaseException as exc:
        record["error"] = type(exc).__name__
        record["cancelled"] = not isinstance(exc, Exception)
        raise
    finally:
        record["ms"] = round((monotonic() - started) * 1000, 3)
        steps.append(record)
