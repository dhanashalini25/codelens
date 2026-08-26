"""Provider-agnostic LLM client with a JSON-extraction helper.

No vendor SDK: one httpx call covers every OpenAI-compatible endpoint, and
Gemini gets a small adapter. The `mock` provider keeps the whole pipeline
runnable and testable offline - with it, a review returns the static findings
only, which is the honest offline behaviour rather than a fake AI result.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, List, Optional

import httpx

from .config import Settings, settings as default_settings


class LLMError(RuntimeError):
    pass


@dataclass
class Message:
    role: str
    content: str

    def to_dict(self) -> dict:
        return {"role": self.role, "content": self.content}


def extract_json(text: str) -> Any:
    """Pull a JSON object out of a model response.

    Models wrap JSON in prose and code fences no matter how firmly the prompt
    says not to. Rather than fight that, parse defensively.
    """
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fall back to the outermost balanced object or array.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"Model did not return valid JSON. First 300 chars: {text[:300]}")


class LLMClient:
    def __init__(self, config: Optional[Settings] = None) -> None:
        self.config = config or default_settings
        self.provider = (self.config.llm_provider or "mock").lower()

    @property
    def is_mock(self) -> bool:
        return self.provider == "mock"

    def complete(
        self, messages: List[Message], temperature: float = 0.1, json_mode: bool = False
    ) -> str:
        if self.provider == "mock":
            return self._mock(messages)
        if self.provider in {"openai", "groq", "openrouter", "together", "mistral", "ollama"}:
            return self._openai_compatible(messages, temperature, json_mode)
        if self.provider in {"gemini", "google"}:
            return self._gemini(messages, temperature, json_mode)
        raise LLMError(f"Unknown provider '{self.provider}'.")

    def complete_json(self, messages: List[Message], temperature: float = 0.0) -> Any:
        """Get JSON back, with two defences and one retry.

        First defence: ask the provider for native JSON mode, which constrains
        decoding so prose is not a possible output.

        Second defence: if it still comes back malformed - some endpoints
        ignore the flag, and any model can be derailed by a diff that contains
        prompt-shaped text - re-ask once with the failure quoted back. One
        retry, not a loop: if the second attempt also fails, the caller needs
        to know rather than watch the token budget drain.
        """
        raw = self.complete(messages, temperature=temperature, json_mode=True)
        try:
            return extract_json(raw)
        except LLMError as first_failure:
            retry = list(messages) + [
                Message("assistant", raw[:500]),
                Message(
                    "user",
                    "That was not valid JSON. Reply with ONLY the JSON object - "
                    "no prose, no code fence, no explanation. If you have nothing "
                    'to report, reply exactly {"findings": []}.',
                ),
            ]
            try:
                return extract_json(self.complete(retry, temperature=0.0, json_mode=True))
            except LLMError:
                raise first_failure from None

    # -- providers ------------------------------------------------------
    def _openai_compatible(
        self, messages: List[Message], temperature: float, json_mode: bool = False
    ) -> str:
        if not self.config.llm_api_key and self.provider != "ollama":
            raise LLMError(
                f"CODELENS_LLM_API_KEY is not set for provider '{self.provider}'. "
                "Set it in .env, or use CODELENS_LLM_PROVIDER=mock to run without a key."
            )
        headers = {"Content-Type": "application/json"}
        if self.config.llm_api_key:
            headers["Authorization"] = f"Bearer {self.config.llm_api_key}"

        url = self.config.llm_base_url.rstrip("/") + "/chat/completions"
        payload: dict = {
            "model": self.config.llm_model,
            "messages": [m.to_dict() for m in messages],
            "temperature": temperature,
        }
        if json_mode:
            # Supported by Groq, OpenAI, Together and most compatible gateways.
            # Endpoints that do not support it reject the request, so fall back
            # rather than losing the call - extract_json still handles prose.
            payload["response_format"] = {"type": "json_object"}
        try:
            response = httpx.post(
                url, json=payload, headers=headers, timeout=self.config.llm_timeout
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Any 400 while JSON mode is on: retry without it. The first
            # version of this checked the error body for "response_format",
            # which failed in practice - Groq returned a 400 with an empty
            # body and the review fell over instead of degrading. Never gate a
            # fallback on the wording of someone else's error message.
            if json_mode and exc.response.status_code == 400:
                return self._openai_compatible(messages, temperature, json_mode=False)

            # Providers do not always explain themselves. An empty 400 body is
            # almost always a bad model name - a decommissioned or renamed
            # model - so say that rather than printing a blank error and
            # leaving the reader with nothing to act on.
            body = exc.response.text.strip()
            if not body:
                body = (
                    f"(empty response body) - most often a bad or decommissioned "
                    f"model name. Current model: '{self.config.llm_model}'. "
                    f"List what your key can reach:  curl -H \"Authorization: Bearer "
                    f"$KEY\" {self.config.llm_base_url.rstrip('/')}/models"
                )
            raise LLMError(
                f"{exc.response.status_code} from {url}: {body[:400]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Could not reach {url}: {exc}") from exc

        data = response.json()
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, AttributeError) as exc:
            raise LLMError(f"Unexpected response shape: {str(data)[:400]}") from exc

    def _gemini(
        self, messages: List[Message], temperature: float, json_mode: bool = False
    ) -> str:
        if not self.config.llm_api_key:
            raise LLMError("CODELENS_LLM_API_KEY is not set for the Gemini provider.")

        system_parts = [m.content for m in messages if m.role == "system"]
        turns = [
            {"role": "model" if m.role == "assistant" else "user", "parts": [{"text": m.content}]}
            for m in messages
            if m.role != "system"
        ]
        generation_config: dict = {"temperature": temperature}
        if json_mode:
            generation_config["responseMimeType"] = "application/json"
        payload: dict = {"contents": turns, "generationConfig": generation_config}
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}

        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.config.llm_model}:generateContent"
        )
        try:
            response = httpx.post(
                url,
                json=payload,
                headers={"x-goog-api-key": self.config.llm_api_key},
                timeout=self.config.llm_timeout,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise LLMError(
                f"{exc.response.status_code} from Gemini: {exc.response.text[:400]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Could not reach Gemini: {exc}") from exc

        data = response.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError) as exc:
            raise LLMError(f"Unexpected Gemini response: {str(data)[:400]}") from exc

    def _mock(self, messages: List[Message]) -> str:
        """Offline stand-in.

        It returns well-formed output of the right shape for each task, without
        inventing review findings. An offline review therefore reports exactly
        what the static rules found - which is true - rather than fabricating
        an AI opinion, which would make the demo a lie.
        """
        prompt = "\n".join(m.content for m in messages)

        if "TASK: review" in prompt:
            return json.dumps(
                {
                    "findings": [],
                    "note": "mock provider - static analysis only, no model was called",
                }
            )

        if "TASK: explain" in prompt:
            return (
                "[mock provider] Set CODELENS_LLM_PROVIDER to a real provider for a "
                "generated explanation. The static structure of this code - its symbols, "
                "sizes and complexity scores - is shown above and is computed locally."
            )

        if "TASK: docstring" in prompt:
            return json.dumps(
                {
                    "docstrings": [],
                    "note": "mock provider - set a real provider to generate docstrings",
                }
            )

        if "TASK: tests" in prompt:
            return json.dumps(
                {
                    "tests": [],
                    "note": "mock provider - set a real provider to generate test recommendations",
                }
            )

        return "[mock provider] No model was called."
