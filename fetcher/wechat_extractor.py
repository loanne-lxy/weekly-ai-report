"""
微信公众号提取器 — 通过 we-mp-rss 服务获取公众号文章。

前置要求：
1. docker-compose.yml 中取消 we-mp-rss 服务的注释
2. 首次运行需要微信扫码登录（we-mp-rss 会提示）
3. 登录后 cookie 保存在 ./data/we-mp-rss/ 目录

使用方式：
在 sources.yaml 中添加：
  - name: "你的公众号名"
    type: wechat
    url: "公众号微信号或URL"
    active: true

we-mp-rss API 文档:
  GET http://localhost:8080/mp/getarticles?name=公众号名&page=1
  返回: { "items": [ { "title", "url", "digest", "pubDate", ... } ] }
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from fetcher.base_extractor import BaseExtractor

logger = logging.getLogger(__name__)


class WechatExtractor(BaseExtractor):
    """
    微信公众号文章提取器。

    调用本地 we-mp-rss 服务的 REST API 获取文章列表。
    """

    name = "wechat"
    DEFAULT_API_BASE = "http://we-mp-rss:8080"  # docker-compose 内部地址

    async def extract(
        self, session: aiohttp.ClientSession, source: dict
    ) -> list[Any]:  # type: ignore[override]
        # 从 source 配置获取公众号名
        mp_name = source.get("wechat_mp", source.get("name", ""))
        if not mp_name:
            logger.warning(f"Wechat: missing wechat_mp config in source: {source.get('name', '?')}")
            return []

        # 优先用 docker-compose 内部地址，fallback 到本地
        api_base = source.get("wechat_api_base", self.DEFAULT_API_BASE)

        # 检查 we-mp-rss 服务是否可用
        try:
            health_url = f"{api_base}/actuator/health"
            async with session.get(health_url, timeout=5) as resp:
                if resp.status != 200:
                    logger.warning(
                        f"Wechat: we-mp-rss health check failed (HTTP {resp.status}), "
                        f"is the service running? ({health_url})"
                    )
                    return []
        except Exception as e:
            logger.warning(
                f"Wechat: cannot connect to we-mp-rss at {api_base} — {e}\n"
                f"  → Start with: docker compose up -d we-mp-rss\n"
                f"  → Then login: open http://localhost:8080/ to scan QR code"
            )
            return []

        # 调用 we-mp-rss API 获取文章
        items = []
        try:
            # 获取最近 20 篇
            for page in range(1, 3):  # 最多2页
                url = f"{api_base}/mp/getarticles?name={mp_name}&page={page}"
                async with session.get(url, timeout=30) as resp:
                    if resp.status != 200:
                        logger.warning(f"Wechat [{mp_name}]: API returned HTTP {resp.status}")
                        break
                    data = await resp.json()
                    articles = data.get("items", [])
                    if not articles:
                        break
                    items.extend(articles)

        except Exception as e:
            logger.warning(f"Wechat [{mp_name}]: API error: {e}")
            return []

        # 转为标准格式
        preview_limit = BaseExtractor.get_preview_limit("wechat")
        results = []
        for art in items[:20]:
            raw = {
                "url": art.get("url", ""),
                "title": art.get("title", ""),
                "summary": art.get("digest", art.get("description", "")),
                "published": art.get("pubDate"),
                "author": art.get("author", ""),
                "source_name": f"WeChat: {mp_name}",
                "source_type": "wechat",
                "feed_url": f"{api_base}/mp/getarticles?name={mp_name}",
                "content_preview": BaseExtractor.truncate_content(
                    art.get("content", art.get("digest", "")), "wechat"
                ),
                "raw_extra": {
                    k: v for k, v in art.items()
                    if k not in {"url", "title", "digest", "description", "pubDate", "author", "content"}
                },
            }
            validated = BaseExtractor.validate(raw)
            if validated:
                results.append(validated)

        logger.info(f"Wechat [{mp_name}]: {len(results)} articles from {len(items)} raw items")
        return results
