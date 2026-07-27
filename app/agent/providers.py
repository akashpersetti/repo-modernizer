import json
import logging
import time

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    pass


class BedrockProvider:
    def __init__(self, model_id: str, name: str, client):
        self.model_id = model_id
        self.name = name
        self.client = client

    def invoke(self, prompt: str) -> tuple[str, int, int]:
        response = self.client.invoke_model(
            modelId=self.model_id,
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}],
            }),
        )
        payload = json.loads(response["body"].read())
        text = payload["content"][0]["text"]
        usage = payload.get("usage", {})
        return text, usage.get("input_tokens", 0), usage.get("output_tokens", 0)


class ProviderRouter:
    def __init__(
        self,
        primary: BedrockProvider,
        fallback: BedrockProvider,
        max_attempts: int = 3,
        backoff_base: float = 1.0,
    ):
        self.primary = primary
        self.fallback = fallback
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base

    def generate(self, prompt: str) -> tuple[str, int, int, str]:
        last_exc: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                text, tokens_in, tokens_out = self.primary.invoke(prompt)
                return text, tokens_in, tokens_out, self.primary.name
            except Exception as exc:  # noqa: BLE001 - deliberately broad, any provider failure triggers failover
                last_exc = exc
                logger.warning("primary provider attempt %d failed: %s", attempt, exc)
                if attempt < self.max_attempts and self.backoff_base:
                    time.sleep(self.backoff_base * (2 ** (attempt - 1)))
        try:
            text, tokens_in, tokens_out = self.fallback.invoke(prompt)
            logger.warning("falling back to %s after primary exhausted retries", self.fallback.name)
            return text, tokens_in, tokens_out, self.fallback.name
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"both providers failed: primary={last_exc}, fallback={exc}") from exc
