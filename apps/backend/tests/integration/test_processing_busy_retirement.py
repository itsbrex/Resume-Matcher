"""Busy processing completion must retain cleanup ownership after HTTP return."""

import asyncio
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text, update

from app.database import Database
from app.main import app
from app.models import Resume
from app.routers import resumes
from tests.integration.test_storage_busy_writes import fast_busy_database  # noqa: F401


@pytest.mark.parametrize("superseded", [False, True])
async def test_busy_processing_finish_is_retired_after_caller_returns(
    fast_busy_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    sample_resume: dict[str, Any],
    superseded: bool,
) -> None:
    database = fast_busy_database
    row = await database.create_resume(
        content="Synthetic resume", processing_status="failed"
    )
    writer = database._session()
    await writer.__aenter__()
    monkeypatch.setattr(resumes, "_PROCESSING_CLEANUP_TIMEOUT_SECONDS", 0.03)

    async def parsed_with_writer_held(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        await writer.execute(text("BEGIN IMMEDIATE"))
        return sample_resume

    monkeypatch.setattr(resumes, "parse_resume_to_json", parsed_with_writer_held)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await asyncio.wait_for(
                client.post(f"/api/v1/resumes/{row['resume_id']}/retry-processing"),
                timeout=0.5,
            )
        assert response.status_code == 503
        assert response.headers["retry-after"] == "1"
        assert resumes._PROCESSING_CLEANUP_TASKS
        if superseded:
            await writer.execute(
                update(Resume)
                .where(Resume.resume_id == row["resume_id"])
                .values(
                    processing_token=None,
                    processing_status="ready",
                    processed_data={"summary": "newer saved result"},
                )
            )
        await writer.commit()
    finally:
        await writer.__aexit__(None, None, None)
        await asyncio.wait_for(
            asyncio.gather(*resumes._PROCESSING_CLEANUP_TASKS), timeout=1
        )

    stored = await database.get_resume(row["resume_id"])
    assert stored is not None
    assert stored["processing_status"] == ("ready" if superseded else "failed")
    assert stored["processed_data"] == (
        {"summary": "newer saved result"} if superseded else None
    )
    async with database._session() as session:
        saved = await session.get(Resume, row["resume_id"])
        assert saved is not None and saved.processing_token is None


async def test_busy_claim_preserves_the_existing_processing_owner(
    fast_busy_database: Database,
) -> None:
    database = fast_busy_database
    row = await database.create_resume(
        content="Synthetic resume", processing_status="failed"
    )
    token = await database.claim_resume_processing(row["resume_id"])
    assert token is not None
    async with database._session() as writer:
        await writer.execute(text("BEGIN IMMEDIATE"))
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/api/v1/resumes/{row['resume_id']}/retry-processing"
            )
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert await database.finish_resume_processing(
        row["resume_id"],
        token,
        processing_status="ready",
        processed_data={"summary": "original owner"},
    ) == "committed"
