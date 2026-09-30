"""Thin wrapper around the openai SDK, pointed at the LiteLLM gateway.

Rule: all model calls go through LiteLLM - never call cluster node IPs
directly for models.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from openai import APIConnectionError, APITimeoutError, OpenAI

from jarvis.config import LiteLLMSettings

logger = logging.getLogger("jarvis.litellm")

REQUEST_TIMEOUT_SECONDS = 180.0

# A streamed answer sometimes dies mid-flight with "connection forcibly closed" while the model
# keeps generating on the node: the backend is fine, the HTTP connection to the gateway is not.
# Seen roughly once in 20 runs from a workstation on the LAN, never yet on SOKRATES-1 itself.
# One fresh attempt costs about as much as the first one and turns a FAILED run into a normal one.
# Timeouts are NOT retried: those already waited the full REQUEST_TIMEOUT_SECONDS.
_CONNECTION_RETRIES = 1


@dataclass
class CompletionResult:
    content: str
    prompt_tokens: int
    completion_tokens: int
    latency_seconds: float


class LiteLLMClient:
    def __init__(self, settings: LiteLLMSettings) -> None:
        self._client = OpenAI(
            base_url=settings.base_url,
            api_key=settings.api_key,
            timeout=REQUEST_TIMEOUT_SECONDS,
            max_retries=0,
        )

    def complete(
        self,
        model_alias: str,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        response_format: dict | None = None,
        stream: bool = True,
    ) -> CompletionResult:
        """stream=True keeps the timeout per chunk instead of per whole generation.

        Use stream=False for models whose gateway route returns an empty stream
        (jarvis-review does).
        """
        start = time.monotonic()
        if stream:
            content, prompt_tokens, completion_tokens = self._streamed_with_retry(
                model_alias, messages, temperature, response_format
            )
        else:
            response = self._client.chat.completions.create(
                model=model_alias,
                messages=messages,
                temperature=temperature,
                response_format=response_format,
            )
            content = response.choices[0].message.content or ""
            usage = response.usage
            prompt_tokens = usage.prompt_tokens if usage else 0
            completion_tokens = usage.completion_tokens if usage else 0
        latency = time.monotonic() - start

        logger.info(
            "model_call model=%s prompt_tokens=%s completion_tokens=%s latency_s=%.2f",
            model_alias,
            prompt_tokens,
            completion_tokens,
            latency,
        )

        return CompletionResult(
            content=content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_seconds=latency,
        )

    def _streamed_with_retry(
        self,
        model_alias: str,
        messages: list[dict[str, str]],
        temperature: float,
        response_format: dict | None,
    ) -> tuple[str, int, int]:
        for attempt in range(_CONNECTION_RETRIES + 1):
            try:
                return self._complete_streamed(model_alias, messages, temperature, response_format)
            except APITimeoutError:
                raise
            except APIConnectionError as exc:
                if attempt == _CONNECTION_RETRIES:
                    raise
                logger.warning(
                    "model_call model=%s: stream broke (%s: %s), retrying once",
                    model_alias, type(exc.__cause__ or exc).__name__, str(exc.__cause__ or exc)[:120],
                )
        raise AssertionError("unreachable")

    def _complete_streamed(
        self,
        model_alias: str,
        messages: list[dict[str, str]],
        temperature: float,
        response_format: dict | None,
    ) -> tuple[str, int, int]:
        stream = self._client.chat.completions.create(
            model=model_alias,
            messages=messages,
            temperature=temperature,
            stream=True,
            stream_options={"include_usage": True},
            response_format=response_format,
        )
        parts: list[str] = []
        prompt_tokens = 0
        completion_tokens = 0
        for chunk in stream:
            if chunk.usage:
                prompt_tokens = chunk.usage.prompt_tokens
                completion_tokens = chunk.usage.completion_tokens
            if chunk.choices and chunk.choices[0].delta.content:
                parts.append(chunk.choices[0].delta.content)
        return "".join(parts), prompt_tokens, completion_tokens
