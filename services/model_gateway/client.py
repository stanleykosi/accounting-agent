"""
Purpose: Implement the internal model gateway abstraction above OpenRouter for bounded LLM access.
Scope: Provider-agnostic client with retry, timeout, and typed-response validation. This module
owns the single canonical path for all model calls used by accounting recommendation workflows.
Dependencies: httpx, pydantic, settings, tenacity, structured logging.

Design notes:
- This is NOT a fake abstraction. It is the concrete OpenRouter-backed client used today.
- A future MCP-ready boundary (Step 48) may wrap this interface, but no extra indirection
  exists now because there is only one provider.
- All model output must be validated against Pydantic schemas before any state mutation.
"""

from __future__ import annotations

import json
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError
from services.common.logging import get_logger
from services.common.settings import get_settings
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)


class ModelGatewayError(Exception):
    """Represent a hard failure in the model gateway that requires explicit recovery."""


class ModelResponseValidationError(ModelGatewayError):
    """Represent a model output that failed schema validation."""

    def __init__(self, *, errors: list[dict[str, Any]], raw_response: str) -> None:
        """Capture validation errors alongside the raw response for operator diagnosis."""
        self.errors = errors
        self.raw_response = raw_response
        formatted_errors = json.dumps(errors, indent=2)
        message = (
            f"Model output failed schema validation.\n"
            f"Validation errors:\n{formatted_errors}\n"
            f"Raw response (first 500 chars):\n{raw_response[:500]}"
        )
        super().__init__(message)


class ModelGatewayRateLimitError(ModelGatewayError):
    """Represent a rate-limit response from the model provider."""

    def __init__(self, *, retry_after_seconds: int | None = None) -> None:
        self.retry_after_seconds = retry_after_seconds
        message = "Model provider rate limit exceeded."
        if retry_after_seconds is not None:
            message += f" Retry after {retry_after_seconds}s."
        super().__init__(message)


class ModelGatewayConfig:
    """Capture per-invocation model routing and behavioral configuration."""

    def __init__(
        self,
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        top_p: float = 1.0,
        timeout_seconds: int | None = None,
    ) -> None:
        """Initialize gateway config with safe defaults for deterministic reasoning.

        Args:
            model: Override the default model. Uses settings default when None.
            temperature: Sampling temperature. Keep at 0.0 for deterministic outputs.
            max_tokens: Maximum completion tokens.
            top_p: Nucleus sampling parameter.
            timeout_seconds: Override the default request timeout.
        """
        settings = get_settings()
        self.model = model or settings.model_gateway.default_model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.top_p = top_p
        self.timeout_seconds = timeout_seconds or settings.model_gateway.timeout_seconds


class ModelGateway:
    """Route bounded reasoning tasks to OpenRouter-backed models with typed validation.

    This class enforces:
    - fail-fast behavior on missing API keys
    - retry with exponential backoff on transient errors
    - schema validation on every model response
    - credential-safe request handling (no key leakage in logs)
    """

    def __init__(self, *, config: ModelGatewayConfig | None = None) -> None:
        """Create a gateway client with optional per-call config overrides.

        Args:
            config: Optional invocation-scoped configuration override.
        """
        self._config = config or ModelGatewayConfig()
        self._settings = get_settings()
        self._require_api_key()

    def _require_api_key(self) -> None:
        """Fail fast when the OpenRouter API key is not configured."""
        if self._settings.model_gateway.api_key is None:
            raise ModelGatewayError(
                "OpenRouter API key is not configured. "
                "Set MODEL_GATEWAY_API_KEY in the environment before running model-backed flows."
            )

    @property
    def base_url(self) -> str:
        """Return the canonical OpenRouter-compatible base URL."""
        return self._settings.model_gateway.base_url

    @property
    def api_key(self) -> str:
        """Return the raw API key value for request construction."""
        key = self._settings.model_gateway.api_key
        if key is None:
            raise ModelGatewayError(
                "OpenRouter API key is not configured. "
                "Set MODEL_GATEWAY_API_KEY in the environment before running model-backed flows."
            )
        return key.get_secret_value()

    @retry(
        retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.ConnectError)),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def _post_completion_request(
        self,
        *,
        messages: list[dict[str, str]],
        request_body_overrides: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Send one chat completion request to the provider with retry semantics.

        Args:
            messages: OpenAI-format message list with role/content.
            request_body_overrides: Optional request-shape overrides for
                structured outputs or provider routing requirements.

        Returns:
            Raw httpx response for downstream parsing.

        Raises:
            ModelGatewayRateLimitError: On 429 responses.
            httpx.HTTPStatusError: On other non-200 responses.
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://127.0.0.1:8000",
            "X-Title": "accounting-ai-agent",
        }
        body = {
            "model": self._config.model,
            "messages": messages,
            "temperature": self._config.temperature,
            "max_tokens": self._config.max_tokens,
            "top_p": self._config.top_p,
        }
        if request_body_overrides:
            body.update(request_body_overrides)

        logger.debug(
            "model_request_start",
            model=self._config.model,
            message_count=len(messages),
            timeout_seconds=self._config.timeout_seconds,
        )

        with httpx.Client(timeout=self._config.timeout_seconds) as client:
            response = client.post(
                url=f"{self.base_url}/chat/completions",
                headers=headers,
                json=body,
            )

        if response.status_code == 429:
            retry_after = None
            retry_header = response.headers.get("retry-after")
            if retry_header is not None:
                try:
                    retry_after = int(retry_header)
                except ValueError:
                    pass
            raise ModelGatewayRateLimitError(retry_after_seconds=retry_after)

        response.raise_for_status()
        return response

    def complete(
        self,
        *,
        messages: list[dict[str, str]],
    ) -> str:
        """Send a chat completion request and return the raw assistant content string.

        Args:
            messages: OpenAI-format message list with role/content.

        Returns:
            The assistant message content string from the first choice.

        Raises:
            ModelGatewayError: On provider failures or missing content.
        """
        try:
            response = self._post_completion_request(messages=messages)
        except httpx.ConnectError as error:
            raise ModelGatewayError(
                f"Failed to connect to model provider at {self.base_url}. "
                "Check network connectivity and the provider endpoint."
            ) from error
        except httpx.HTTPStatusError as error:
            raise ModelGatewayError(
                f"Model provider returned HTTP {error.response.status_code}. "
                f"Response body: {error.response.text[:300]}"
            ) from error

        payload = response.json()
        choices = payload.get("choices", [])
        if not choices:
            raise ModelGatewayError(
                "Model provider returned no choices in the completion response. "
                "This can happen when the model is unavailable or the request is malformed."
            )

        content: str = choices[0].get("message", {}).get("content", "")
        if not content:
            raise ModelGatewayError(
                "Model provider returned an empty assistant message. "
                "Check the prompt and model configuration."
            )

        logger.debug(
            "model_response_received",
            model=self._config.model,
            content_length=len(content),
        )

        return content

    def complete_structured(
        self,
        *,
        messages: list[dict[str, str]],
        response_model: type[T],
    ) -> T:
        """Send a chat completion request and validate the response against a Pydantic model.

        The LLM must return valid JSON that parses into the target model. This is the
        canonical path for all state-mutating model outputs.

        Args:
            messages: OpenAI-format message list with role/content.
            response_model: Pydantic model type for response validation.

        Returns:
            Validated Pydantic model instance.

        Raises:
            ModelResponseValidationError: When the response does not match the schema.
            ModelGatewayError: On provider failures or JSON parse errors.
        """
        try:
            response = self._post_completion_request(
                messages=messages,
                request_body_overrides=_build_structured_output_request(response_model),
            )
        except httpx.ConnectError as error:
            raise ModelGatewayError(
                f"Failed to connect to model provider at {self.base_url}. "
                "Check network connectivity and the provider endpoint."
            ) from error
        except httpx.HTTPStatusError as error:
            raise ModelGatewayError(
                f"Model provider returned HTTP {error.response.status_code}. "
                f"Response body: {error.response.text[:300]}"
            ) from error

        payload = response.json()
        choices = payload.get("choices", [])
        if not choices:
            raise ModelGatewayError(
                "Model provider returned no choices in the completion response. "
                "This can happen when the model is unavailable or the request is malformed."
            )

        raw_content: str = choices[0].get("message", {}).get("content", "")
        if not raw_content:
            raise ModelGatewayError(
                "Model provider returned an empty assistant message. "
                "Check the prompt and model configuration."
            )

        # Strip markdown code fences if the model wrapped JSON in them
        parsed_content = _strip_markdown_fences(raw_content)

        try:
            parsed_json = json.loads(parsed_content)
        except json.JSONDecodeError as error:
            raise ModelGatewayError(
                f"Model response was not valid JSON.\n"
                f"Response (first 500 chars):\n{raw_content[:500]}"
            ) from error

        try:
            return response_model.model_validate(parsed_json)
        except ValidationError as error:
            # error.errors() returns list[dict[str, Any]] from pydantic-core
            raise ModelResponseValidationError(
                errors=list(error.errors()),  # type: ignore[arg-type]
                raw_response=raw_content,
            ) from error


def _build_structured_output_request(response_model: type[BaseModel]) -> dict[str, Any]:
    """Build the canonical request overrides for schema-enforced completions.

    Structured planning is the single current-state path for agent outputs that
    drive workflow behavior. We require providers that honor every parameter so
    OpenRouter does not route a planning request to a provider that silently
    ignores the JSON schema contract.
    """

    return {
        "provider": {
            "require_parameters": True,
        },
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": response_model.__name__,
                "strict": True,
                "schema": response_model.model_json_schema(),
            },
        },
    }


def _strip_markdown_fences(content: str) -> str:
    """Remove markdown JSON code fences from LLM responses.

    Some models wrap JSON output in ```json...``` blocks. This helper
    strips those fences so downstream JSON parsing succeeds reliably.

    Args:
        content: Raw LLM response string.

    Returns:
        Cleaned JSON string ready for parsing.
    """
    stripped = content.strip()
    if stripped.startswith("```"):
        # Remove the opening fence (``` or ```json)
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
        # Remove the closing fence
        if stripped.endswith("```"):
            stripped = stripped[:-3].rstrip()
    return stripped


def get_gateway(*, config: ModelGatewayConfig | None = None) -> ModelGateway:
    """Create a model gateway instance with optional configuration overrides.

    Args:
        config: Optional invocation-scoped configuration override.

    Returns:
        Configured ModelGateway ready for completion calls.
    """
    return ModelGateway(config=config)


__all__ = [
    "ModelGateway",
    "ModelGatewayConfig",
    "ModelGatewayError",
    "ModelGatewayRateLimitError",
    "ModelResponseValidationError",
    "get_gateway",
]
