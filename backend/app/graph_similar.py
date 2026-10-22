from __future__ import annotations

from typing import Any

import os
import time

import httpx

from app.config import get_env
from app.logging_config import get_logger


logger = get_logger("bigdipper.graph_similar")


class SimilarPaperService:
    def __init__(self) -> None:
        self.embed_url = (get_env("BIGDIPPER_EMBEDDING_BASE_URL", "http://127.0.0.1:18081") or "http://127.0.0.1:18081").rstrip("/")
        self.milvus_host = get_env("BIGDIPPER_MILVUS_HOST", "127.0.0.1") or "127.0.0.1"
        self.milvus_port = get_env("BIGDIPPER_MILVUS_PORT", "19531") or "19531"
        self.collection = get_env("BIGDIPPER_MILVUS_COLLECTION", "sciscinet_cn_paper_embeddings_hnsw") or "sciscinet_cn_paper_embeddings_hnsw"
        self.vector_field = get_env("BIGDIPPER_MILVUS_VECTOR_FIELD", "embedding") or "embedding"
        self.primary_key_field = get_env("BIGDIPPER_MILVUS_PRIMARY_KEY_FIELD", "paperid") or "paperid"
        self.timeout = float(get_env("BIGDIPPER_SIMILAR_TIMEOUT_SECONDS", "2") or "2")

    def search(self, query: str, limit: int = 10, seed_paper_id: str | None = None) -> tuple[list[dict[str, Any]], list[str]]:
        try:
            warnings: list[str] = []
            embedding: list[float] | None = None
            if seed_paper_id:
                try:
                    embedding = self._paper_embedding(seed_paper_id)
                except Exception as exc:
                    logger.info("similar_seed_vector=unavailable paper_id=%s reason=%s", seed_paper_id, exc)
                    warnings.append("HelmDB has no stored vector for this paper; used title-based similarity instead.")
            if embedding is None:
                embedding = self._embed(query)
            results = self._milvus_search(embedding, limit + (1 if seed_paper_id else 0), exclude_ids={seed_paper_id} if seed_paper_id else set())
            if results:
                return results[:limit], warnings
            return [], [*warnings, "HelmDB returned no similar papers; returned fallback results."]
        except Exception as exc:
            logger.info("similar_search=fallback reason=%s", exc)
            return [], ["HelmDB similar-paper search is temporarily unavailable; returned fallback results."]

    def _embed(self, query: str) -> list[float]:
        with httpx.Client(trust_env=False, timeout=self.timeout) as client:
            response = client.post(f"{self.embed_url}/embed", json={"query": query})
            response.raise_for_status()
            payload = response.json()
        embedding = payload.get("embedding") or payload.get("vector")
        if not isinstance(embedding, list):
            raise RuntimeError("embedding service response did not contain embedding")
        return [float(value) for value in embedding]

    def _paper_embedding(self, paper_id: str) -> list[float]:
        started = time.monotonic()
        client = self._client()
        rows = client.get(
            collection_name=self.collection,
            ids=[paper_id],
            output_fields=[self.vector_field],
        )
        row = rows[0] if rows else None
        if not isinstance(row, dict):
            raise RuntimeError(f"paper vector not found for {paper_id}")
        embedding = row.get(self.vector_field)
        if not isinstance(embedding, list):
            raise RuntimeError(f"paper vector not found for {paper_id}")
        logger.info("similar_seed_vector=end elapsed=%.3f paper_id=%s", time.monotonic() - started, paper_id)
        return [float(value) for value in embedding]

    def _client(self) -> Any:
        try:
            from pymilvus import MilvusClient
        except Exception as exc:
            raise RuntimeError("pymilvus is not installed") from exc
        _ensure_no_proxy(self.milvus_host)
        return MilvusClient(uri=f"http://{self.milvus_host}:{self.milvus_port}")

    def _milvus_search(self, embedding: list[float], limit: int, exclude_ids: set[str] | None = None) -> list[dict[str, Any]]:
        started = time.monotonic()
        client = self._client()
        results = client.search(
            collection_name=self.collection,
            data=[embedding],
            limit=max(1, min(limit, 50)),
            anns_field=self.vector_field,
            search_params={"metric_type": "COSINE", "params": {"ef": 128}},
            output_fields=[self.primary_key_field],
        )
        excluded = exclude_ids or set()
        items: list[dict[str, Any]] = []
        for hit in (results[0] if results else []):
            entity = hit.get("entity") if isinstance(hit, dict) else getattr(hit, "entity", {})
            paper_id = None
            if isinstance(entity, dict):
                paper_id = entity.get(self.primary_key_field)
            if not paper_id and isinstance(hit, dict):
                paper_id = hit.get(self.primary_key_field) or hit.get("id")
            if not paper_id or str(paper_id) in excluded:
                continue
            score = _hit_score(hit)
            items.append({"paper_id": str(paper_id), "rank": len(items) + 1, "score": score})
        logger.info("similar_milvus_search=end elapsed=%.3f rows=%d", time.monotonic() - started, len(items))
        return items


def _ensure_no_proxy(host: str) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return
    required = ["127.0.0.1", "localhost", "::1"]
    for key in ("NO_PROXY", "no_proxy"):
        current = os.environ.get(key, "")
        parts = [part.strip() for part in current.split(",") if part.strip()]
        for value in required:
            if value not in parts:
                parts.append(value)
        os.environ[key] = ",".join(parts)


def _hit_score(hit: Any) -> float | None:
    if isinstance(hit, dict):
        value = hit.get("score", hit.get("distance"))
    else:
        value = getattr(hit, "score", None)
        if value is None:
            value = getattr(hit, "distance", None)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
