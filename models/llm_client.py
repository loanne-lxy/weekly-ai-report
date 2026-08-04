"""统一 LLM 调用接口 — 支持 Ollama / OpenAI / DeepSeek / 自定义"""
import os
from pathlib import Path
from openai import OpenAI

try:
    from langsmith import wrappers
    _wrap_openai = wrappers.wrap_openai
except ImportError:
    _wrap_openai = None


def _load_dotenv():
    """加载项目根目录的 .env 文件"""
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

        if not api_key or api_key == "ollama":
            raise RuntimeError(
                "API key not found. Set DEEPSEEK_API_KEY in .env file "
                "or export DEEPSEEK_API_KEY=xxx"
            )

        raw_client = OpenAI(
            api_key=api_key,
            base_url=cfg.get("base_url", "http://localhost:11434/v1"),
        )

        # LangSmith 追踪（如果已配置）
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
        prompt = f"""判断以下文章属于哪个领域。只输出领域名称，不要解释。

领域定义:
- LLM: 核心大模型技术（新模型架构/训练方法/推理优化/基准评测/对齐技术/Transformer变体/量化/蒸馏），不包含应用案例
- Agent: AI智能体（自主决策/工具调用/多Agent协作/RAG/机器人/任务规划/人机交互Agent），不含纯模型发布
- AI for Science: AI驱动科学发现（蛋白质结构/药物研发/材料科学/气象预测/数学证明/物理模拟）
- 设计仿真: AI辅助工程设计（生成式设计/CAD/CAE仿真/3D生成/渲染/Omniverse/数字内容创作）
- 数字孪生: 工业数字孪生（IoT数据集成/实时仿真/预测性维护/工业4.0/赛博物理系统）

如果都不属于，输出 "NONE"。

标题: {title}
摘要: {summary[:500]}

领域:"""
        result = self.chat(system_prompt="你是AI资讯分类器。严格按照领域定义分类，不要将AI应用案例归入LLM。", user_prompt=prompt)
        for name in cat_names:
            if name in result:
                return name
        return None

    def summarize(self, title: str, content: str) -> str:
        prompt = f"""用中文写一段100字以内的摘要，提取核心观点和关键信息。

标题: {title}
内容: {content[:2000]}

摘要(中文,100字内):"""
        return self.chat(
            system_prompt="你是AI资讯摘要员。用简洁中文总结，不超过100字。",
            user_prompt=prompt,
        )
