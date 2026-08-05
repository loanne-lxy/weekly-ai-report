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

        api_key = cfg.get("api_key", "")
        if api_key.startswith("${") and api_key.endswith("}"):
            api_key = os.environ.get(api_key[2:-1], "")

        if self.provider != "ollama" and (not api_key or api_key == "ollama"):
            raise RuntimeError(
                "API key not found. Set DEEPSEEK_API_KEY in .env file "
                "or export DEEPSEEK_API_KEY=xxx"
            )

        raw_client = OpenAI(
            api_key=api_key,
            base_url=cfg.get("base_url", "http://localhost:11434/v1"),
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
        )
        return response.choices[0].message.content.strip()
