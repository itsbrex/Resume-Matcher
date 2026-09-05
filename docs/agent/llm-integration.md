# LLM Integration Guide

> **Multi-provider AI support, JSON handling, and prompt guidelines.**

## Multi-Provider Support

Backend uses LiteLLM to support multiple providers through a unified API:

| Provider | Type | Notes |
|----------|------|-------|
| **Ollama** | Local | Free, runs on your machine |
| **OpenAI** | Cloud | GPT-5 Nano, GPT-4o |
| **Azure AI Foundry** | Cloud | Azure AI Inference / Foundry model endpoints |
| **Anthropic** | Cloud | Claude Haiku 4.5 |
| **Google Gemini** | Cloud | Gemini 3 Flash |
| **OpenRouter** | Cloud | Access to multiple models |
| **DeepSeek** | Cloud | DeepSeek Chat |

## API Key Handling

API keys are passed directly to `litellm.acompletion()` via the `api_key` parameter (not via `os.environ`) to avoid race conditions in async contexts.

```python
# Correct
await litellm.acompletion(
    model=model,
    messages=messages,
    api_key=api_key  # Direct parameter
)

# Incorrect - don't use os.environ in async code
os.environ["OPENAI_API_KEY"] = key  # Race condition risk
```

## JSON Mode

The `complete_json()` function automatically enables `response_format={"type": "json_object"}` for providers that support it:

- OpenAI
- Anthropic
- Gemini
- DeepSeek
- Major OpenRouter models

## Retry Logic

JSON completions include 2 automatic retries with progressively lower temperature:

- Attempt 1: temperature 0.1
- Attempt 2: temperature 0.0

## Temperature Support

`_supports_temperature()` determines whether `temperature` is sent at all. Capability comes from LiteLLM's model registry, with overrides for restrictions the registry does not record.

The gpt-5 family is one such restriction. The registry lists `temperature` as a supported parameter, which is accurate, but only the default value of 1 is accepted ([litellm#13397](https://github.com/BerriAI/litellm/issues/13397)). This holds on OpenAI and on Azure alike.

The override matches on model name rather than provider. Azure gpt-5 deployments resolve to `azure/` prefixed names, and a self-hosted `openai_compatible` model under a custom name is absent from the registry and already treated as unsupported before the override runs.

`_get_retry_temperature()` therefore returns `None` for gpt-5, omitting the parameter so the provider applies its own default.

## JSON Extraction

Robust bracket-matching algorithm in `_extract_json()` handles:

- Malformed responses
- Markdown code blocks
- Edge cases
- Infinite recursion protection when content starts with `{` but matching fails

## Error Handling Pattern

LLM functions log detailed errors server-side but return generic messages to clients:

```python
except Exception as e:
    logger.error(f"LLM completion failed: {e}")
    raise ValueError("LLM completion failed. Please check your API configuration.")
```

## Adding Prompts

Add new prompt templates to `apps/backend/app/prompts/templates.py`.

### Prompt Guidelines

1. Use `{variable}` for substitution (single braces)
2. Include example JSON schemas for structured outputs
3. Keep instructions concise: "Output ONLY the JSON object, no other text"

### Example

```python
IMPROVE_BULLET = """
Improve this resume bullet point for a {job_title} position.

Current: {current_bullet}

Output ONLY the improved bullet point, no explanations.
"""
```

## Provider Configuration

Users configure their preferred AI provider via:

- Settings page: `/settings`
- API: `PUT /api/v1/config/llm-api-key`

Azure AI Foundry uses LiteLLM's `azure_ai/` route for generic Azure AI Inference endpoints. Foundry-hosted Azure OpenAI endpoints such as `https://<resource>.services.ai.azure.com/openai/v1/responses` are normalized to the service root and routed through LiteLLM's `azure/` provider automatically. Set `LLM_PROVIDER=azure_foundry`, `LLM_MODEL` to the Foundry model or deployment name, `LLM_API_BASE` to the Azure AI endpoint, and store the Foundry API key in the `azure_foundry` key slot.

## Health Checks

The `/api/v1/health` endpoint validates LLM connectivity.

> **Note**: Docker health checks must use `/api/v1/health` (not `/health`).

## Timeouts

All LLM calls have configurable timeouts:

| Operation | Timeout |
|-----------|---------|
| Health checks | 30s |
| Completions | 120s |
| JSON operations | 180s |

## Key Files

| File | Purpose |
|------|---------|
| `apps/backend/app/llm.py` | LiteLLM wrapper with JSON mode |
| `apps/backend/app/prompts/templates.py` | Prompt templates |
| `apps/backend/app/prompts/enrichment.py` | Enrichment-specific prompts |
| `apps/backend/app/config.py` | Provider configuration |
