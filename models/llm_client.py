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
        self.temperature = cfg.get("temperature", 0.3)
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

    def classify(
        self, title: str, summary: str, categories: list[dict]
    ) -> str | None:
        cat_names = [c["name"] for c in categories]
        prompt = f"""Classify the following article into exactly one domain. Output ONLY the domain name, nothing else.

Domain definitions:
- LLM: Core LLM technology (model architecture, training methods, inference optimization, benchmarks, alignment, Transformer variants, quantization, distillation). NOT application stories.
- Agent: AI agents (autonomous decision-making, tool calling, multi-agent collaboration, RAG, robotics, task planning, human-agent interaction). NOT pure model releases.
- AI for Science: AI-driven scientific discovery (protein structure, drug R&D, materials science, weather prediction, mathematical proofs, physics simulation).
- 设计仿真: AI-assisted engineering design (generative design, CAD, CAE simulation, 3D generation, rendering, Omniverse, digital content creation).
- 数字孪生: Industrial digital twins (IoT data integration, real-time simulation, predictive maintenance, Industry 4.0, cyber-physical systems).

If none match, output "NONE".

Title: {title}
Summary: {summary[:500]}

Domain:"""
        result = self.chat(
            system_prompt="You are an AI article classifier. Strictly follow the domain definitions. Do NOT classify application stories as LLM.",
            user_prompt=prompt,
        )
        for name in cat_names:
            if name in result:
                return name
        return None

    def summarize(self, title: str, content: str) -> str:
        return self.chat(
            system_prompt="You are an AI summarizer. Write concise Chinese summaries.",
            user_prompt=(
                f"Write a one-sentence Chinese summary (under 80 characters) capturing the key insight:\n\n"
                f"Title: {title}\n"
                f"Content: {content[:2000]}\n\n"
                f"Chinese summary:"
            ),
        )
