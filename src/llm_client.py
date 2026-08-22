"""Single OpenAI-compatible JSON client with bounded retries."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any
import yaml

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None

ROOT = Path(__file__).resolve().parents[1]

def _load_dotenv() -> None:
    """Load simple KEY=VALUE pairs without adding a dotenv dependency."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

def load_config() -> dict[str, Any]:
    _load_dotenv()
    with (ROOT / "config" / "config.yaml").open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

class LLMClient:
    def __init__(self, config: dict[str, Any] | None = None, client: Any = None):
        cfg = (config or load_config()).get("llm", {})
        self.base_url = os.getenv("LLM_BASE_URL", cfg.get("base_url", "https://api.deepseek.com/v1"))
        self.model = os.getenv("LLM_MODEL", cfg.get("model", "deepseek-chat"))
        self.temperature = float(os.getenv("LLM_TEMPERATURE", cfg.get("temperature", 0.1)))
        self.api_key = os.getenv(cfg.get("api_key_env", "LLM_API_KEY"), "")
        self._client = client
        if self._client is None and OpenAI and self.api_key:
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    def json_completion(self, messages: list[dict[str, str]], retries: int = 3) -> Any:
        last_error = None
        for attempt in range(retries):
            try:
                if self._client is None:
                    raise RuntimeError("LLM_API_KEY 未设置，无法调用模型")
                response = self._client.chat.completions.create(
                    model=self.model, messages=messages, temperature=self.temperature,
                    response_format={"type": "json_object"},
                )
                content = response.choices[0].message.content
                payload = json.loads(content)
                if not isinstance(payload, dict):
                    raise ValueError("LLM JSON 顶层必须是对象")
                return payload
            except Exception as exc:
                last_error = exc
                if attempt < retries - 1:
                    time.sleep(0.2 * (attempt + 1))
        raise RuntimeError(f"LLM JSON 调用失败（{retries}次）: {last_error}")

def call_json(messages: list[dict[str, str]], client: LLMClient | None = None) -> Any:
    return (client or LLMClient()).json_completion(messages)
