# tests/test_providers.py
import json
from unittest.mock import MagicMock

import pytest

from app.agent.providers import BedrockProvider, ProviderError, ProviderRouter


def _bedrock_response(text: str, tokens_in: int = 10, tokens_out: int = 20) -> MagicMock:
    body = MagicMock()
    body.read.return_value = json.dumps({
        "content": [{"text": text}],
        "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
    }).encode()
    return {"body": body}


def test_bedrock_provider_invoke_parses_response():
    client = MagicMock()
    client.invoke_model.return_value = _bedrock_response("hello world")
    provider = BedrockProvider("model-id", "bedrock-primary", client)

    text, tokens_in, tokens_out = provider.invoke("prompt")

    assert text == "hello world"
    assert tokens_in == 10
    assert tokens_out == 20


def test_router_uses_primary_on_success():
    primary_client = MagicMock()
    primary_client.invoke_model.return_value = _bedrock_response("primary result")
    fallback_client = MagicMock()

    router = ProviderRouter(
        BedrockProvider("primary-id", "bedrock-primary", primary_client),
        BedrockProvider("fallback-id", "bedrock-fallback", fallback_client),
        max_attempts=2,
        backoff_base=0,
    )

    text, tokens_in, tokens_out, provider_name = router.generate("prompt")

    assert text == "primary result"
    assert provider_name == "bedrock-primary"
    fallback_client.invoke_model.assert_not_called()


def test_router_falls_back_after_primary_exhausts_retries():
    primary_client = MagicMock()
    primary_client.invoke_model.side_effect = RuntimeError("throttled")
    fallback_client = MagicMock()
    fallback_client.invoke_model.return_value = _bedrock_response("fallback result")

    router = ProviderRouter(
        BedrockProvider("primary-id", "bedrock-primary", primary_client),
        BedrockProvider("fallback-id", "bedrock-fallback", fallback_client),
        max_attempts=2,
        backoff_base=0,
    )

    text, tokens_in, tokens_out, provider_name = router.generate("prompt")

    assert text == "fallback result"
    assert provider_name == "bedrock-fallback"
    assert primary_client.invoke_model.call_count == 2


def test_router_raises_provider_error_when_both_fail():
    primary_client = MagicMock()
    primary_client.invoke_model.side_effect = RuntimeError("primary down")
    fallback_client = MagicMock()
    fallback_client.invoke_model.side_effect = RuntimeError("fallback down")

    router = ProviderRouter(
        BedrockProvider("primary-id", "bedrock-primary", primary_client),
        BedrockProvider("fallback-id", "bedrock-fallback", fallback_client),
        max_attempts=2,
        backoff_base=0,
    )

    with pytest.raises(ProviderError):
        router.generate("prompt")
