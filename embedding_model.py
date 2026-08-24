"""Centralized FastEmbed model loading and cache verification.

This module keeps both event clustering and semantic deduplication pointed at
the same stable cache directory. It also works around a fastembed 0.8
behaviour where a partial HuggingFace snapshot is accepted even though
``model_optimized.onnx`` is missing.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
HF_MODEL_REPO = "qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q"

# Files that must be present before we consider the local cache usable.
REQUIRED_FILES = (
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "model_optimized.onnx",
)

# ``preprocessor_config.json`` is optional for this model, but harmless to
# allow if the upstream repository publishes it.
ALLOW_PATTERNS = [*REQUIRED_FILES, "preprocessor_config.json"]

_model_cache: dict[str, Any] = {}

# Recent HuggingFace Hub versions use Xet by default. It has been observed to
# stall on this project's network, so prefer the regular HTTP downloader unless
# the caller explicitly asks for Xet.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_ENDPOINT", "https://huggingface.co")


def _default_cache_dir() -> Path:
    configured = os.getenv("FASTEMBED_CACHE_PATH") or os.getenv("EMBEDDING_CACHE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".cache" / "fastembed"


def get_cache_dir() -> Path:
    """Return the stable cache directory, creating it if needed."""
    cache_dir = _default_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _snapshot_is_complete(snapshot_dir: str | Path) -> bool:
    model_dir = Path(snapshot_dir)
    for relative_path in REQUIRED_FILES:
        candidate = model_dir / relative_path
        if not candidate.is_file() or candidate.stat().st_size == 0:
            return False
    return True


def _find_complete_local_snapshot() -> Path | None:
    """Return a complete local snapshot without touching the network."""
    try:
        from huggingface_hub import snapshot_download

        snapshot = snapshot_download(
            repo_id=HF_MODEL_REPO,
            allow_patterns=ALLOW_PATTERNS,
            cache_dir=str(get_cache_dir()),
            local_files_only=True,
        )
    except Exception:
        return None

    if _snapshot_is_complete(snapshot):
        return Path(snapshot)
    return None


def ensure_model_files() -> Path:
    """Ensure all required ONNX/tokenizer files exist in the local cache.

    The common path performs a local-only lookup first, so a healthy cache
    does not trigger a network request. Missing or zero-byte files trigger a
    one-time download.
    """
    local_snapshot = _find_complete_local_snapshot()
    if local_snapshot is not None:
        return local_snapshot

    from huggingface_hub import snapshot_download

    cache_dir = get_cache_dir()
    logger.info("Embedding model cache incomplete; downloading required files to %s", cache_dir)
    try:
        snapshot = snapshot_download(
            repo_id=HF_MODEL_REPO,
            allow_patterns=ALLOW_PATTERNS,
            cache_dir=str(cache_dir),
            local_files_only=False,
        )
    except Exception as exc:
        raise RuntimeError(
            "Could not download the embedding model. "
            "Set HF_ENDPOINT=https://huggingface.co and retry."
        ) from exc

    if not _snapshot_is_complete(snapshot):
        raise RuntimeError(f"Embedding model snapshot is incomplete: {snapshot}")
    return Path(snapshot)


def get_embedding_model(model_name: str = DEFAULT_MODEL_NAME) -> Any:
    """Return a cached ``TextEmbedding`` instance for the configured model."""
    if model_name not in _model_cache:
        if model_name == DEFAULT_MODEL_NAME:
            ensure_model_files()

        from fastembed import TextEmbedding

        model = TextEmbedding(
            model_name=model_name,
            cache_dir=str(get_cache_dir()),
            local_files_only=True,
        )
        _model_cache[model_name] = model
    return _model_cache[model_name]


def model_directory(model: Any) -> Path | None:
    """Return the resolved snapshot directory used by a FastEmbed model."""
    raw_path = getattr(model, "_model_dir", None) or getattr(model, "model_dir", None)
    if not raw_path:
        return None
    return Path(raw_path)


def tokenizer_path(model: Any) -> Path | None:
    """Return the tokenizer path associated with a FastEmbed model."""
    model_dir = model_directory(model)
    if model_dir is None:
        return None
    candidate = model_dir / "tokenizer.json"
    return candidate if candidate.is_file() else None
