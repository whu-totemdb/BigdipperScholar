"""Vector SQL construction for the unified database; no external vector store."""
import math
import re

from app.config import get_env


def vector_settings() -> dict:
    cn = get_env("BIGDIPPER_DATA_PROFILE", "generic") == "cn"
    return {
        "table": get_env("BIGDIPPER_VECTOR_TABLE") or ("cn_paper_embeddings_full_hnsw" if cn else None),
        "idColumn": get_env("BIGDIPPER_VECTOR_ID_COLUMN") or "paperid",
        "vectorColumn": get_env("BIGDIPPER_VECTOR_COLUMN") or "embedding",
        "dimensions": int(get_env("BIGDIPPER_EMBEDDING_DIMENSIONS") or (768 if cn else 0)) or None,
        "model": get_env("BIGDIPPER_EMBEDDING_MODEL") or None,
        "metric": "cosine", "operator": "<=>", "backend": "mmdb",
    }


def _identifier(value: str | None) -> str:
    if not value or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?", value):
        raise ValueError("Configure a valid MMDB vector table/column identifier")
    return ".".join('"' + part + '"' for part in value.split("."))


def validate_vector(vector) -> None:
    if (not isinstance(vector, list) or not vector
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector)):
        raise ValueError("Embedding must be a nonempty finite numeric vector")
    dimensions = vector_settings()["dimensions"]
    if dimensions and len(vector) != dimensions:
        raise ValueError("Query embedding dimensions do not match corpus dimensions")


def seed_vector_sql() -> str:
    config = vector_settings()
    table, pid, vector = (_identifier(config[k]) for k in ("table", "idColumn", "vectorColumn"))
    return f"SELECT {vector}::text AS embedding FROM {table} WHERE {pid} = %(paper_id)s LIMIT 1"


def vector_search_sql() -> str:
    config = vector_settings()
    table, pid, vector = (_identifier(config[k]) for k in ("table", "idColumn", "vectorColumn"))
    return f"""SELECT {pid} AS paperid,
        {vector} <=> %(embedding)s::vector AS distance,
        1 - ({vector} <=> %(embedding)s::vector) AS score
        FROM {table}
        WHERE (%(paper_id)s IS NULL OR {pid} <> %(paper_id)s)
        ORDER BY {vector} <=> %(embedding)s::vector
        LIMIT %(limit)s"""
