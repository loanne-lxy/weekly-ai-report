"""Unified LLM client — supports Ollama / OpenAI / DeepSeek / custom providers"""
import os
from pathlib import Path
from openai import OpenAI

try:
    from langsmith import wrappers
    _wrap_openai = wrappers.wrap_openai
except ImportError:
    _wrap_openai = None


def _load_dotenv():
    """Load .env file from project root"""
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()


class LLMClient:
    def __init__(self, config: dict):
        cfg = config["model"]
        self.provider = cfg.get("provider", "openai")

        api_key = cfg.get("api_key", "") or ""
        if api_key.startswith("${") and api_key.endswith("}"):
            api_key = os.environ.get(api_key[2:-1], "")

        if self.provider not in ("ollama", "custom") and (not api_key or api_key == "ollama"):
            raise RuntimeError(
                "API key not found. Set DEEPSEEK_API_KEY in .env file "
                "or export DEEPSEEK_API_KEY=xxx"
            )

        raw_client = OpenAI(
            api_key=api_key or "none",  # "none" is fine for custom endpoints
            base_url=cfg.get("base_url", "http://localhost:11434/v1"),
            timeout=cfg.get("timeout", 120),  # seconds per LLM call
            max_retries=5,  # retry on transient failures
        )

        # LangSmith tracing (if configured)
        if _wrap_openai and os.environ.get("LANGSMITH_TRACING") == "true":
            self.client = _wrap_openai(raw_client)
        else:
            self.client = raw_client

        self.model = cfg.get("name", "qwen3:14b")
        self.temperature = 0  # deterministic for consistent results
        self.max_tokens = cfg.get("max_tokens", 2048)

    def chat(self, system_prompt: str, user_prompt: str) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            # Qwen3.8 is a reasoning model: on SHORT structured-JSON calls
            # thinking is pure overhead — disable it. NOTE: the endpoint
            # IGNORES this on long inputs (observed: identical reasoning
            # output with it on/off at ~2.7k prompt tokens), so long calls
            # still think and eat max_tokens — that's why max_tokens is
            # sized for thinking, not just the JSON answer.
            extra_body={"enable_thinking": False},
        )
        message = response.choices[0].message
        # Some models (e.g. Qwen reasoning) put output in reasoning + content
        parts = []
        if getattr(message, "refusal", None):
            parts.append(message.refusal)
        if message.content:
            parts.append(message.content)
        # For reasoning models: append reasoning if content is empty
        reasoning = getattr(message, "reasoning", None)
        if not parts and reasoning:
            parts.append(reasoning)
        result = "\n".join(parts)
        return result.strip() if result else ""
