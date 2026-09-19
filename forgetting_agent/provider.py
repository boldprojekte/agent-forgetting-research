"""Thin OpenAI-compatible provider adapter (live runs only).

Settings come from an explicit .env file loaded with python-dotenv; real environment
variables override the file. The API key is held by the SDK client only and never appears in
`describe()`, in requests bodies, or in traces.
"""

from __future__ import annotations

import math
import os
import random as random_module
import re
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx2 as httpx
from dotenv import dotenv_values
from openai import APIConnectionError, APIStatusError, OpenAI
from openai.types.chat import ChatCompletion

from .streaming import ChatStream, TruncatedStreamError

DEFAULT_TIMEOUT_SECONDS = 120.0
GLM_53_MAX_OUTPUT_TOKENS = 131_072
ENV_KEYS = ("TENSORX_API_KEY", "TENSORX_BASE_URL", "TENSORX_MODEL")

# TensorX thinking toggle (docs.tensorx.ai/api-reference/reasoning): the boolean lives inside
# `chat_template_kwargs` under a key that differs per model family, and a wrong key is silently
# ignored by the provider. Only the families this project has run are mapped; keyed by model ID
# prefix. Anything else is refused rather than sent with a guessed key.
THINKING_KEYS = {"deepseek/deepseek-v4": "thinking", "z-ai/glm": "enable_thinking"}


def documented_max_output_tokens(model: str) -> int:
    """Return a verified model-family ceiling; refuse unknown models instead of guessing."""
    normalized = model.removeprefix("z-ai/").lower()
    if normalized.startswith("glm-5.3"):
        return GLM_53_MAX_OUTPUT_TOKENS
    raise ValueError(f"no verified maximum output allowance is recorded for model {model!r}")


def thinking_extra_body(model: str, enabled: bool) -> dict[str, Any]:
    """`extra_body` that turns visible reasoning on/off for `model`, or ValueError if unmapped."""
    for prefix, key in THINKING_KEYS.items():
        if model.startswith(prefix):
            return {"chat_template_kwargs": {key: enabled}}
    mapped = ", ".join(f"{p}* -> chat_template_kwargs.{k}" for p, k in THINKING_KEYS.items())
    raise ValueError(
        f"--thinking has no known request key for model {model!r}; mapped families: {mapped}"
    )


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    model: str


def load_settings(env_path: Path | None = None) -> Settings:
    """Read TENSORX_* from `env_path` (default: ./.env), letting the environment win."""
    path = Path(env_path) if env_path is not None else Path.cwd() / ".env"
    file_values = dotenv_values(path) if path.is_file() else {}
    values = {key: os.environ.get(key) or file_values.get(key) or "" for key in ENV_KEYS}
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise RuntimeError(
            f"Missing settings: {', '.join(missing)}. Put them in {path} or the environment."
        )
    return Settings(
        api_key=values["TENSORX_API_KEY"],
        base_url=values["TENSORX_BASE_URL"],
        model=values["TENSORX_MODEL"],
    )


def response_to_dict(response: ChatCompletion) -> dict[str, Any]:
    """Provider-native dict of the response; message keeps extra fields (reasoning_content).

    None-valued fields are dropped from the message except `content`, which stays as an
    explicit null so the assistant message can be sent back unchanged.

    `metadata` is copied from the parsed object as-is instead of being serialized through the
    SDK schema: the SDK types it as `Dict[str, str]`, but TensorX (GLM) returns nested lists in
    it. The SDK parses leniently and keeps the raw value, and serializing that value against
    the schema raised a Pydantic warning per response. Only this field bypasses the schema;
    every other warning would still surface.
    """
    data = response.model_dump(mode="json", exclude={"metadata"})
    data["metadata"] = getattr(response, "metadata", None)
    for choice in data.get("choices", []):
        message = choice.get("message") or {}
        choice["message"] = {
            key: value for key, value in message.items() if value is not None or key == "content"
        }
    return data


def _retryable(error: Exception | None) -> bool:
    if isinstance(error, (APIConnectionError, httpx.TransportError, TruncatedStreamError)):
        return True
    if not isinstance(error, APIStatusError):
        return False
    if error.status_code == 429:
        body = error.body if isinstance(error.body, dict) else {}
        detail = body.get("error", body)
        if isinstance(detail, dict) and any(
            detail.get(key)
            in ("insufficient_quota", "billing_hard_limit_reached", "credit_balance_exhausted")
            for key in ("code", "type")
        ):
            return False
        return True
    return error.status_code in (500, 502, 503, 504)


def _retry_after(error: Exception | None) -> float:
    if not isinstance(error, APIStatusError):
        return 0.0
    text = error.response.headers.get("retry-after", "")
    try:
        seconds = float(text)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(text).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return 0.0
    return seconds if math.isfinite(seconds) and seconds >= 0 else 0.0


class OpenAIModel:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        *,
        max_attempts: int = 6,
        streaming: bool = True,
        supports_images: bool = False,
        backoff_cap: float = 60.0,
        max_retry_wait: float = 900.0,
        sleep: Callable[[float], None] = time.sleep,
        random: Callable[[], float] = random_module.random,
    ) -> None:
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        if not math.isfinite(backoff_cap) or backoff_cap <= 0:
            raise ValueError("backoff_cap must be positive and finite")
        if not math.isfinite(max_retry_wait) or max_retry_wait <= 0:
            raise ValueError("max_retry_wait must be positive and finite")
        self.max_retry_wait = max_retry_wait
        self._redact = lambda text: re.sub(
            r"(?i)bearer\s+\S+", "Bearer [redacted]", str(text).replace(api_key, "[redacted]")
        )[:2000]
        self.pricing: dict[str, Any] | None = None
        self.streaming = streaming
        self.supports_images = supports_images
        self.name = model
        self._base_url = base_url
        self._timeout = timeout
        self.max_attempts = max_attempts
        self.backoff_cap = backoff_cap
        self._sleep = sleep
        self._random = random
        self.last_attempts: list[dict[str, Any]] = []
        # Only our explicit retry loop may issue additional requests.
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)

    def describe(self) -> dict[str, Any]:
        return {
            "provider": "openai-compatible",
            "pricing": self.pricing,
            "base_url": self._base_url,
            "model": self.name,
            "timeout_seconds": self._timeout,
            "max_attempts": self.max_attempts,
            "backoff_cap_seconds": self.backoff_cap,
            "max_retry_wait_seconds": self.max_retry_wait,
            "rate_limit_min_wait_seconds": 30,
            "retry_interrupted_transport": True,
            "sdk_max_retries": 0,
            "streaming": self.streaming,
            "supports_images": self.supports_images,
        }

    def prepare_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """Expose episode transport options before the request is sized and traced.

        complete() itself sends the supplied body unchanged, including historical replays.
        """
        body = deepcopy(request)
        body.setdefault("stream", self.streaming)
        if body["stream"]:
            body.setdefault("stream_options", {"include_usage": True})
        return body

    def complete(
        self,
        request: dict[str, Any],
        *,
        on_attempt: Callable[[dict[str, Any]], None] | None = None,
        on_chunk: Callable[[dict[str, Any]], None] | None = None,
        attempt_limit: int | None = None,
    ) -> dict[str, Any]:
        """Retry only transport failures, before any response or tool is accepted.

        A private snapshot and fresh copy per attempt keep the logical body unchanged.
        Timeouts can still incur server costs; this is not server-side idempotency.
        """
        body = deepcopy(request)
        self.last_attempts = []
        if not self.supports_images and any(
            part.get("type") == "image_url"
            for message in body.get("messages", [])
            if isinstance(message.get("content"), list)
            for part in message["content"]
        ):
            raise ValueError(
                "Image input is not enabled for this provider/model. "
                "Select a verified vision-capable model and set supports_images=True."
            )
        if attempt_limit is not None and (type(attempt_limit) is not int or attempt_limit < 1):
            raise ValueError("attempt_limit must be a positive integer")
        limit = (
            min(self.max_attempts, attempt_limit)
            if attempt_limit is not None
            else self.max_attempts
        )
        waited = 0.0
        ceiling = min(1.0, self.backoff_cap)
        for attempt in range(1, limit + 1):
            started = time.monotonic()
            error = None
            stream_opened = False
            observed_usage = None
            chunks = 0
            first_chunk_seconds = None
            last_chunk_seconds = None
            try:
                response = self._client.chat.completions.create(**deepcopy(body))
                if body.get("stream"):
                    stream_opened = True
                    accumulator = ChatStream()
                    with response as stream:
                        for chunk in stream:
                            data = chunk.model_dump(mode="json", exclude_unset=True)
                            if data.get("usage") is not None:
                                observed_usage = data["usage"]
                            chunks += 1
                            last_chunk_seconds = time.monotonic() - started
                            if first_chunk_seconds is None:
                                first_chunk_seconds = last_chunk_seconds
                            if on_chunk is not None:
                                on_chunk(
                                    {
                                        "attempt": attempt,
                                        "chunk": chunks,
                                        "elapsed_seconds": last_chunk_seconds,
                                        "body": data,
                                    }
                                )
                            accumulator.add(data)
                    response = accumulator.finish()
                else:
                    usage = getattr(response, "usage", None)
                    if usage is not None:
                        observed_usage = usage.model_dump(mode="json")
            except Exception as caught:  # No response has entered history or executed a tool yet
                error = caught
            elapsed = time.monotonic() - started
            retryable = _retryable(error)
            will_retry = retryable and attempt < limit
            wait = 0.0
            if will_retry:
                wait = ceiling * max(0.0, min(1.0, self._random()))
                if getattr(error, "status_code", None) == 429:
                    # Even zero jitter must leave a rate-limit recovery interval.
                    wait += min(30.0 * 2 ** (attempt - 1), 120.0)
                elif stream_opened:
                    wait = max(wait, 5.0)
                wait = max(wait, _retry_after(error))
                if waited + wait > self.max_retry_wait:
                    will_retry = False
            ceiling = min(self.backoff_cap, ceiling * 2)
            stop_reason = None
            if retryable and not will_retry and waited + wait > self.max_retry_wait:
                stop_reason = "retry_wait_budget"
                wait = 0.0
            elif retryable and not will_retry:
                stop_reason = (
                    "physical_request_cap"
                    if attempt_limit is not None and limit == attempt_limit
                    else "attempt_cap"
                )
            if error is None:
                outcome = "success"
            elif will_retry:
                outcome = "retry"
            else:
                outcome = "exhausted" if retryable else "permanent_error"
            details = {}
            if isinstance(error, APIStatusError):
                raw = error.body if isinstance(error.body, dict) else {}
                raw = raw.get("error", raw)
                if isinstance(raw, dict):
                    details = {
                        k: self._redact(raw[k]) for k in ("code", "type", "message") if k in raw
                    }
                details["headers"] = {
                    k: self._redact(v)
                    for k, v in error.response.headers.items()
                    if k.lower() in ("retry-after", "x-request-id", "request-id")
                    or k.lower().startswith(("x-ratelimit-", "ratelimit-"))
                }
            event = {
                "usage": observed_usage,
                "attempt": attempt,
                "streaming": bool(body.get("stream")),
                "chunks": chunks,
                "first_chunk_seconds": first_chunk_seconds,
                "last_chunk_seconds": last_chunk_seconds,
                "error_type": type(error).__name__ if error else None,
                "error_details": details,
                "stream_interrupted": bool(error and stream_opened),
                "elapsed_seconds": elapsed,
                "wait_seconds": wait,
                "status_code": getattr(error, "status_code", None),
                "stop_reason": stop_reason,
                "outcome": outcome,
            }
            self.last_attempts.append(event)
            if on_attempt is not None:
                on_attempt(dict(event))
            if error is None:
                return response if body.get("stream") else response_to_dict(response)
            if not will_retry:
                raise error
            self._sleep(wait)
            waited += wait
        raise AssertionError("unreachable")
