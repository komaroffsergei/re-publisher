from __future__ import annotations

from dataclasses import dataclass
import asyncio
import json
from pathlib import Path
import re
from typing import Any

import typer
import yaml

from app.config import Settings
from app.content.common import run_async, settings_or_exit
from app.content.text_utils import clean_text
from app.main import safe_echo

app = typer.Typer(no_args_is_help=True)

DEFAULT_PROMPT_PATH = Path("config/yandexgpt_prompts.yaml")
SUMMARY_MODEL_NAME = "yandexgpt-5-lite"
REWRITE_MODEL_NAME = "yandexgpt-5.1"
FORBIDDEN_EDITORIAL_PHRASES = [
    "Коротко",
    "source unavailable",
    "Автоматическое извлечение",
    "AI-среды",
]


@dataclass(frozen=True)
class YandexCompletion:
    text: str
    model_uri: str
    usage: dict[str, Any]


class YandexGPTError(RuntimeError):
    pass


@app.callback()
def main() -> None:
    """YandexGPT helper commands."""


def redact(value: str | None) -> str:
    if not value:
        return "<missing>"
    return f"<redacted len={len(value)}>"


def extract_yandex_keys_from_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    in_section = False
    current_key: str | None = None
    key_map = {"secret": "YANDEX_API_KEY", "api": "YANDEX_API_KEY_ID", "folder": "YANDEX_FOLDER_ID"}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line == "yandexgpt_keys:":
            in_section = True
            current_key = None
            continue
        if not in_section:
            continue
        if line.endswith(":"):
            candidate = line[:-1].strip()
            if candidate in key_map:
                current_key = candidate
                continue
            if values:
                break
        if current_key:
            values[key_map[current_key]] = line
            current_key = None
    return values


def map_yandex_key_file(path: Path) -> dict[str, str]:
    if not path.exists():
        raise YandexGPTError(f"Yandex key file not found: {path}")
    values = extract_yandex_keys_from_text(path.read_text(encoding="utf-8"))
    required = {"YANDEX_API_KEY", "YANDEX_FOLDER_ID"}
    missing = sorted(required - set(values))
    if missing:
        raise YandexGPTError(f"Yandex key file is missing: {', '.join(missing)}")
    return values


def load_prompt_config(path: Path = DEFAULT_PROMPT_PATH) -> dict[str, Any]:
    if not path.exists():
        raise YandexGPTError(f"Prompt config not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise YandexGPTError(f"Prompt config must be a mapping: {path}")
    return data


def label_prompts(config: dict[str, Any]) -> dict[str, str]:
    labels = ((config.get("rewrite") or {}).get("labels") or {})
    return {str(key): clean_text(value) for key, value in labels.items()}


def model_uri(settings: Settings, purpose: str) -> str:
    if purpose == "summary":
        explicit = settings.yandex_summary_model_uri
        default_model = SUMMARY_MODEL_NAME
    elif purpose == "rewrite":
        explicit = settings.yandex_rewrite_model_uri
        default_model = REWRITE_MODEL_NAME
    else:
        raise YandexGPTError(f"Unknown YandexGPT purpose: {purpose}")
    if explicit:
        return explicit
    if not settings.yandex_folder_id:
        raise YandexGPTError("YANDEX_FOLDER_ID is required for YandexGPT model URI.")
    return f"gpt://{settings.yandex_folder_id}/{default_model}"


def model_name_from_uri(uri: str) -> str:
    tail = uri.rstrip("/").split("/")[-1]
    return tail or uri


def require_yandex_settings(settings: Settings) -> None:
    if not settings.enable_external_llm:
        raise YandexGPTError("ENABLE_EXTERNAL_LLM=true is required for YandexGPT calls.")
    if not settings.yandex_api_key:
        raise YandexGPTError("YANDEX_API_KEY is required for YandexGPT.")
    if not settings.yandex_folder_id and not (settings.yandex_summary_model_uri and settings.yandex_rewrite_model_uri):
        raise YandexGPTError("YANDEX_FOLDER_ID or explicit Yandex model URIs are required.")


def parse_completion_response(payload: dict[str, Any], model: str) -> YandexCompletion:
    result = payload.get("result")
    if not isinstance(result, dict):
        raise YandexGPTError("YandexGPT response has no result object.")
    alternatives = result.get("alternatives")
    if not isinstance(alternatives, list) or not alternatives:
        raise YandexGPTError("YandexGPT response has no alternatives.")
    message = alternatives[0].get("message") if isinstance(alternatives[0], dict) else None
    text = message.get("text") if isinstance(message, dict) else None
    cleaned = clean_text(text)
    if not cleaned:
        raise YandexGPTError("YandexGPT response text is empty.")
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    return YandexCompletion(text=cleaned, model_uri=model, usage=usage)


async def complete(
    settings: Settings,
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.2,
    max_tokens: int = 900,
) -> YandexCompletion:
    require_yandex_settings(settings)
    try:
        import httpx
    except ImportError as exc:
        raise YandexGPTError(f"httpx unavailable: {exc}") from exc

    headers = {
        "Authorization": f"Api-Key {settings.yandex_api_key}",
        "Content-Type": "application/json",
        "x-data-logging-enabled": "false",
    }
    payload = {
        "modelUri": model,
        "completionOptions": {
            "stream": False,
            "temperature": temperature,
            "maxTokens": str(max_tokens),
        },
        "messages": [
            {"role": "system", "text": clean_text(system_prompt)},
            {"role": "user", "text": clean_text(user_prompt)},
        ],
    }
    last_error: Exception | None = None
    for attempt in range(settings.yandex_max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=settings.yandex_timeout_seconds) as client:
                response = await client.post(settings.yandex_api_url, headers=headers, json=payload)
            if response.status_code >= 400:
                body = clean_text(response.text)[:500]
                raise YandexGPTError(f"YandexGPT HTTP {response.status_code}: {body}")
            return parse_completion_response(response.json(), model)
        except (httpx.HTTPError, json.JSONDecodeError, YandexGPTError) as exc:
            last_error = exc
            if attempt >= settings.yandex_max_retries:
                break
            await asyncio.sleep(0.5 * (attempt + 1))
    raise YandexGPTError(str(last_error) if last_error else "YandexGPT request failed.")


def clip_prompt_text(text: str | None, max_chars: int) -> str:
    cleaned = clean_text(text)
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars].rsplit(" ", 1)[0].strip()


def build_summary_messages(
    config: dict[str, Any],
    *,
    title: str | None,
    description: str | None,
    extracted_text: str | None,
    max_input_chars: int,
) -> tuple[str, str]:
    summary_config = config.get("summary") or {}
    system = clean_text(summary_config.get("system"))
    user_template = clean_text(summary_config.get("user"))
    if not system or not user_template:
        raise YandexGPTError("Summary prompts are not configured.")
    user = user_template.format(
        title=clean_text(title) or "нет",
        description=clean_text(description) or "нет",
        extracted_text=clip_prompt_text(extracted_text, max_input_chars) or "нет",
    )
    return system, user


def build_rewrite_messages(
    config: dict[str, Any],
    *,
    label: str | None,
    title: str | None,
    post_text: str | None,
    source_summary: str | None,
    source_url: str | None,
    showcase_title: str | None,
    max_input_chars: int,
) -> tuple[str, str]:
    rewrite_config = config.get("rewrite") or {}
    system = clean_text(rewrite_config.get("system"))
    user_template = clean_text(rewrite_config.get("common_user"))
    labels = label_prompts(config)
    label_key = label or "news_digest"
    label_instruction = labels.get(label_key) or labels.get("news_digest") or "Сделай аккуратный редакционный рерайт."
    if not system or not user_template:
        raise YandexGPTError("Rewrite prompts are not configured.")
    user = user_template.format(
        label=label_key,
        label_instruction=label_instruction,
        title=clean_text(title) or "нет",
        post_text=clip_prompt_text(post_text, max_input_chars) or "нет",
        source_summary=clip_prompt_text(source_summary, max_input_chars) or "нет",
        source_url=clean_text(source_url) or "нет",
        showcase_title=clean_text(showcase_title) or "нет",
    )
    return system, user


def strip_json_fence(text: str) -> str:
    cleaned = clean_text(text)
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        cleaned = clean_text(fenced.group(1))
    if not cleaned.startswith("{"):
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            cleaned = cleaned[start : end + 1]
    return cleaned


def sanitize_editorial_text(text: str) -> str:
    cleaned = clean_text(text)
    for phrase in FORBIDDEN_EDITORIAL_PHRASES:
        cleaned = re.sub(rf"{re.escape(phrase)}\s*[:：-]?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return clean_text(cleaned)


def parse_rewrite_response(text: str) -> dict[str, Any]:
    cleaned = strip_json_fence(text)
    try:
        data = json.loads(cleaned, strict=False)
    except json.JSONDecodeError as exc:
        title_match = re.search(r'"title"\s*:\s*"(?P<title>.*?)"\s*,\s*"body"', cleaned, flags=re.DOTALL)
        body_match = re.search(
            r'"body"\s*:\s*"(?P<body>.*?)(?:"\s*,\s*"(?:risk_flags|claims)"|"risk_flags"\s*:|"claims"\s*:)',
            cleaned,
            flags=re.DOTALL,
        )
        if not body_match:
            raise YandexGPTError(f"YandexGPT rewrite JSON parse failed: {exc}") from exc
        data = {
            "title": title_match.group("title") if title_match else "Материал",
            "body": body_match.group("body"),
            "risk_flags": ["json_salvaged"],
            "claims": [],
        }
    if not isinstance(data, dict):
        raise YandexGPTError("YandexGPT rewrite response must be a JSON object.")
    title = sanitize_editorial_text(str(data.get("title") or "Материал"))[:240]
    body = sanitize_editorial_text(str(data.get("body") or ""))
    if not body:
        raise YandexGPTError("YandexGPT rewrite body is empty.")
    raw_risk_flags = data.get("risk_flags") if isinstance(data.get("risk_flags"), list) else []
    risk_flags = [clean_text(str(flag)) for flag in raw_risk_flags if clean_text(str(flag))]
    raw_claims = data.get("claims") if isinstance(data.get("claims"), list) else []
    claims = [claim for claim in raw_claims if isinstance(claim, dict)]
    return {"title": title, "body": body, "risk_flags": risk_flags, "claims": claims}


@app.command("check-credentials")
def check_credentials() -> None:
    """Run a tiny YandexGPT request without printing secrets."""

    settings = settings_or_exit()
    summary_model = model_uri(settings, "summary")
    completion = run_async(
        complete(
            settings,
            model=summary_model,
            system_prompt="Ответь одним словом по-русски.",
            user_prompt="Проверка связи. Верни: готово",
            temperature=0.0,
            max_tokens=16,
        )
    )
    usage = completion.usage or {}
    safe_echo(
        "status=ok "
        f"model={model_name_from_uri(completion.model_uri)} "
        f"usage={json.dumps(usage, ensure_ascii=True, sort_keys=True)}"
    )


if __name__ == "__main__":
    app()
