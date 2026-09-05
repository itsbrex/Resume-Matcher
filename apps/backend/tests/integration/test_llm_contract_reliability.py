"""Real LiteLLM Router retry and structured-content contracts."""

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import litellm
import pytest

from app import llm
from app.llm import LLMConfig
from app.services import improver
from app.routers import enrichment


CONFIG = LLMConfig(provider="openai", model="gpt-4o", api_key="synthetic")


def _error(kind: str) -> Exception:
    error_type = getattr(litellm, kind)
    return error_type(
        message="synthetic transport failure",
        model="gpt-4o",
        llm_provider="openai",
    )


def _response(payload: object) -> litellm.ModelResponse:
    return litellm.ModelResponse(
        model="gpt-4o",
        choices=[
            {
                "message": {"role": "assistant", "content": json.dumps(payload)},
                "finish_reason": "stop",
                "index": 0,
            }
        ],
    )


@pytest.mark.parametrize(
    ("kind", "expected_calls"),
    [
        ("AuthenticationError", 1),
        ("BadRequestError", 1),
        ("ContentPolicyViolationError", 1),
        ("Timeout", 3),
        ("InternalServerError", 3),
        ("RateLimitError", 4),
    ],
)
async def test_real_router_enforces_transport_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    expected_calls: int,
) -> None:
    """Installed Router dispatch must match the documented per-class budget."""
    router = llm._build_router(CONFIG)
    calls = 0

    async def failing_provider(**_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        raise _error(kind)

    monkeypatch.setattr(litellm, "acompletion", failing_provider)
    monkeypatch.setattr(router, "_time_to_sleep_before_retry", lambda **_kwargs: 0)
    monkeypatch.setattr(llm, "get_router", lambda _config=None: (router, CONFIG))
    monkeypatch.setattr(llm, "_supports_json_mode", lambda _model: False)

    with pytest.raises(Exception):
        await llm.complete_json("synthetic", retries=3)

    assert calls == expected_calls


async def test_exhausted_transport_error_does_not_start_content_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Content retries cannot multiply an exhausted Router timeout budget."""
    router = llm._build_router(CONFIG)
    calls = 0

    async def failing_provider(**_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        raise _error("Timeout")

    monkeypatch.setattr(litellm, "acompletion", failing_provider)
    monkeypatch.setattr(router, "_time_to_sleep_before_retry", lambda **_kwargs: 0)
    monkeypatch.setattr(llm, "get_router", lambda _config=None: (router, CONFIG))
    monkeypatch.setattr(llm, "_supports_json_mode", lambda _model: False)

    with pytest.raises(litellm.Timeout):
        await llm.complete_json("synthetic", retries=3)

    assert calls == 3


async def test_recovered_transport_retry_accepts_legitimate_sparse_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty optional sections are schema-valid and must not cause regeneration."""
    router = llm._build_router(CONFIG)
    calls = 0
    sparse = {
        "personalInfo": {"name": "Sparse Candidate"},
        "workExperience": [],
        "education": [],
    }

    async def alternating(**_kwargs: Any) -> litellm.ModelResponse:
        nonlocal calls
        calls += 1
        if calls % 2:
            raise _error("Timeout")
        return _response(sparse)

    monkeypatch.setattr(litellm, "acompletion", alternating)
    monkeypatch.setattr(router, "_time_to_sleep_before_retry", lambda **_kwargs: 0)
    monkeypatch.setattr(llm, "get_router", lambda _config=None: (router, CONFIG))
    monkeypatch.setattr(llm, "_supports_json_mode", lambda _model: False)

    result = await llm.complete_json("synthetic", retries=3, schema_type="resume")

    assert result == sparse
    assert calls == 2


async def test_content_retry_repairs_malformed_optional_keyword_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consumed optional field is repaired before suggestions can slice it."""
    malformed = {
        "required_skills": [],
        "preferred_skills": [],
        "keywords": [],
        "key_responsibilities": None,
    }
    repaired = {
        "required_skills": [],
        "preferred_skills": [],
        "keywords": [],
        "key_responsibilities": ["Lead reliable API delivery"],
    }
    router = AsyncMock()
    router.acompletion.side_effect = [_response(malformed), _response(repaired)]
    monkeypatch.setattr(llm, "get_router", lambda _config=None: (router, CONFIG))
    monkeypatch.setattr(llm, "_supports_json_mode", lambda _model: False)

    result = await llm.complete_json(
        "synthetic job",
        retries=1,
        schema_type="keywords",
        response_validator=improver._validate_keyword_result,
    )
    suggestions = improver.generate_improvements(result)

    assert router.acompletion.await_count == 2
    assert suggestions == [
        {
            "suggestion": "Aligned experience with: Lead reliable API delivery",
            "lineNumber": None,
        }
    ]


def test_enrichment_uses_valid_legacy_field_when_canonical_field_is_empty() -> None:
    result = enrichment._validate_enhancement_result(
        {
            "additional_bullets": [],
            "enhanced_description": ["Improved factual bullet"],
        }
    )

    assert result["additional_bullets"] == ["Improved factual bullet"]


async def test_prose_prefixed_top_level_array_gets_corrective_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = AsyncMock()
    array_response = _response([{"changes": []}])
    array_response.choices[0].message.content = 'Here is the result: [{"changes": []}]'
    router.acompletion.side_effect = [array_response, _response({"changes": []})]
    monkeypatch.setattr(llm, "get_router", lambda _config=None: (router, CONFIG))
    monkeypatch.setattr(llm, "_supports_json_mode", lambda _model: False)
    result = await llm.complete_json("synthetic", retries=1, schema_type="diff")

    assert result == {"changes": []}
    assert router.acompletion.await_count == 2
    retry_messages = router.acompletion.await_args_list[1].kwargs["messages"]
    assert "Output ONLY a valid JSON object" in retry_messages[-1]["content"]


async def test_validator_must_return_an_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = AsyncMock()
    router.acompletion.side_effect = [_response({"ok": True}), _response({"ok": True})]
    monkeypatch.setattr(llm, "get_router", lambda _config=None: (router, CONFIG))
    monkeypatch.setattr(llm, "_supports_json_mode", lambda _model: False)

    with pytest.raises(ValueError, match="Response validator must return a JSON object"):
        await llm.complete_json(
            "synthetic",
            retries=1,
            response_validator=lambda _result: [],  # type: ignore[return-value]
        )

    assert router.acompletion.await_count == 2


async def test_skill_plan_retries_non_text_strategy_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = AsyncMock()
    router.acompletion.side_effect = [
        _response({"target_skills": [], "strategy_notes": False}),
        _response({"target_skills": [], "strategy_notes": "Keep claims grounded"}),
    ]
    monkeypatch.setattr(llm, "get_router", lambda _config=None: (router, CONFIG))
    monkeypatch.setattr(llm, "_supports_json_mode", lambda _model: False)

    result = await llm.complete_json(
        "synthetic",
        retries=1,
        response_validator=improver._validate_skill_plan_result,
    )

    assert result["strategy_notes"] == "Keep claims grounded"
    assert router.acompletion.await_count == 2


async def test_real_router_propagates_cancellation_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller cancellation reaches the provider once and is never classified."""
    router = llm._build_router(CONFIG)
    cancelled = asyncio.Event()
    calls = 0

    async def blocking_provider(**_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(litellm, "acompletion", blocking_provider)
    monkeypatch.setattr(llm, "get_router", lambda _config=None: (router, CONFIG))
    monkeypatch.setattr(llm, "_supports_json_mode", lambda _model: False)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(llm.complete_json("synthetic"), timeout=0.05)

    assert cancelled.is_set()
    assert calls == 1
