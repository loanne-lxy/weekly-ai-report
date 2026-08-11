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
    DEFAULT_API_BASE = "http://we-mp-rss:8001"  # docker-compose 内部地址

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

        # we-mp-rss 认证：Authorization: AK-SK {access_key}:{secret_key}
        access_key = source.get("wechat_access_key", "")
        secret_key = source.get("wechat_secret_key", "")
        if access_key and secret_key:
            headers = {"Authorization": f"AK-SK {access_key}:{secret_key}"}
        else:
            logger.warning(f"Wechat: missing access_key/secret_key in source config")
            return []

        # 检查 we-mp-rss 服务是否可用（通过 /api/feeds 端点）
        try:
            health_url = f"{api_base}/api/feeds"
            async with session.get(health_url, headers=headers, timeout=5) as resp:
                if resp.status == 200:
                    logger.info("Wechat: we-mp-rss service OK")
                else:
                    logger.warning(
                        f"Wechat: we-mp-rss not responding (HTTP {resp.status}) at {health_url}, "
                        f"service running? → sudo docker ps | grep we-mp-rss"
                    )
                    return []
        except Exception as e:
            logger.warning(
                f"Wechat: cannot connect to we-mp-rss at {api_base} — {e}\n"
                f"  → Start: sudo docker run -d --name we-mp-rss -p 8001:8001 rachelos/we-mp-rss:latest\n"
                f"  → Login: http://localhost:8001/ 扫码 → 创建 Access Key"
            )
            return []

        # 获取公众号文章列表
        items = []
        try:
            # 先获取 feed 列表
            feeds_url = f"{api_base}/api/feeds"
            async with session.get(feeds_url, headers=headers, timeout=30) as resp:
                if resp.status == 200:
                    feeds = await resp.json()
                    if not feeds:
                        logger.warning(
                            f"Wechat: no feeds found — articles may still be syncing. "
                            f"Check http://localhost:8001/ for status"
                        )
                        return []

                    # 搜索匹配的公众号
                    target_feed = None
                    for feed in feeds:
                        if mp_name in feed.get("mp_name", ""):
                            target_feed = feed
                            break

                    if not target_feed:
                        logger.warning(
                            f"Wechat [{mp_name}]: not found in feed list. "
                            f"Available: {[f.get('mp_name') for f in feeds[:5]]}"
                        )
                        return []

                    mp_id = target_feed.get("id") or target_feed.get("mp_id")

                    # 获取该公众号的文章（we-mp-rss 1.5.2 使用 articles 端点）
                    article_url = f"{api_base}/api/v1/wx/articles?offset=0&limit=20&mp_id={mp_id}"
                    async with session.get(article_url, headers=headers, timeout=30) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get("code") == 0:
                                items = data.get("data", {}).get("list", [])
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
