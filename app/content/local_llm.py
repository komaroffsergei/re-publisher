from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

import typer

from app.config import Settings
from app.content.common import run_async, settings_or_exit
from app.content.text_utils import clean_text
from app.content.yandex_gpt import YandexGPTError
from app.main import safe_echo

app = typer.Typer(no_args_is_help=True)
SAIGA_NEMO_MODEL_LABEL = "saiga_nemo_12b.Q4_K_M"


@dataclass(frozen=True)
class LocalLLMCompletion:
    text: str
    model: str
    usage: dict[str, Any]


class LocalLLMError(YandexGPTError):
    pass


@app.callback()
def main() -> None:
    """Local OpenAI-compatible LLM helper commands."""


def local_model_label(model: str) -> str:
    lowered = model.lower()
    if "saiga_nemo_12b" in lowered and "q4_k_m" in lowered:
        return SAIGA_NEMO_MODEL_LABEL
    return model.rsplit("/", 1)[-1].replace(":", ".")


def parse_chat_completion_response(payload: dict[str, Any], model: str) -> LocalLLMCompletion:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LocalLLMError("Local LLM response has no choices.")
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    text = clean_text(message.get("content") or first.get("text"))
    if not text:
        raise LocalLLMError("Local LLM response text is empty.")
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    return LocalLLMCompletion(text=text, model=str(payload.get("model") or model), usage=usage)


async def complete(
    settings: Settings,
    *,
    system_prompt: str,
    user_prompt: str,
    temperature: float | None = None,
    max_tokens: int | None = None,
    json_object: bool = False,
) -> LocalLLMCompletion:
    try:
        import httpx
    except ImportError as exc:
        raise LocalLLMError(f"httpx unavailable: {exc}") from exc

    base = settings.local_llm_api_base.rstrip("/")
    payload = {
        "model": settings.local_llm_model,
        "messages": [
            {"role": "system", "content": clean_text(system_prompt)},
            {"role": "user", "content": clean_text(user_prompt)},
        ],
        "temperature": settings.local_llm_temperature if temperature is None else temperature,
        "max_tokens": settings.local_llm_max_tokens if max_tokens is None else max_tokens,
        "stream": False,
    }
    if json_object:
        payload["response_format"] = {"type": "json_object"}
    try:
        async with httpx.AsyncClient(timeout=settings.local_llm_timeout_seconds) as client:
            response = await client.post(f"{base}/chat/completions", json=payload)
        if response.status_code >= 400:
            body = clean_text(response.text)[:500]
            raise LocalLLMError(f"Local LLM HTTP {response.status_code}: {body}")
        return parse_chat_completion_response(response.json(), settings.local_llm_model)
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise LocalLLMError(f"Local LLM request failed: {exc}") from exc


async def check_server() -> LocalLLMCompletion:
    settings = settings_or_exit()
    return await complete(
        settings,
        system_prompt="Ответь одним словом по-русски.",
        user_prompt="Проверка связи. Верни: готово",
        temperature=0.0,
        max_tokens=16,
    )


@app.command("check-server")
def check_server_command() -> None:
    """Run a tiny local LLM request."""

    completion = run_async(check_server())
    safe_echo(
        "status=ok "
        f"model={local_model_label(completion.model)} "
        f"usage={json.dumps(completion.usage or {}, ensure_ascii=True, sort_keys=True)}"
    )


if __name__ == "__main__":
    app()
