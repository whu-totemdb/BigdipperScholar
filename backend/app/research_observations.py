"""Local research bookkeeping. No database or model calls are made here."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


ANALYSIS_TOOL_NAME = "record_observation_analysis"


@dataclass
class ObservationSelection:
    ids: list[int] = field(default_factory=list)
    rationale: str = ""
    finalized: bool = False

    def update(self, ids: list[int], mode: str, rationale: str) -> None:
        if self.finalized:
            raise ValueError("Selection is already finalized")
        if mode not in {"append", "remove", "replace", "finalize"}:
            raise ValueError("Unknown selection mode")
        if mode in {"replace", "finalize"}:
            self.ids = list(dict.fromkeys(ids))
        elif mode == "remove":
            self.ids = [oid for oid in self.ids if oid not in ids]
        else:
            self.ids = list(dict.fromkeys([*self.ids, *ids]))
        self.rationale = rationale
        self.finalized = mode == "finalize"


def record_analyses(raw: Any, observations: list[dict], round_number: int) -> list[dict]:
    """Atomically validate explicit, post-result analyses, then attach to sources."""
    if not isinstance(raw, list):
        raise ValueError("analyses must be a list")
    existing = {item["observationId"]: item for item in observations}
    validated = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Each analysis must be an object")
        ids = item.get("observationIds")
        summary = item.get("summary")
        if (not isinstance(ids, list) or not ids
                or any(type(oid) is not int or oid not in existing for oid in ids)):
            raise ValueError("Analysis must reference already returned observation IDs")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 2000:
            raise ValueError("Analysis summary must contain 1–2000 characters")
        validated.append({"sourceObservationIds": list(dict.fromkeys(ids)),
                          "summary": summary.strip(), "recordedAtRound": round_number})
    for item in validated:
        for oid in item["sourceObservationIds"]:
            entries = existing[oid].setdefault("resultAnalyses", [])
            if item not in entries:
                entries.append(item)
    return validated


def analysis_schema() -> dict:
    return {"type": "array", "items": {
        "type": "object", "properties": {
            "observationIds": {"type": "array", "items": {"type": "integer"}, "minItems": 1},
            "summary": {"type": "string", "minLength": 1, "maxLength": 2000},
        }, "required": ["observationIds", "summary"], "additionalProperties": False,
    }}


def analysis_tool_definition() -> dict:
    return {"type": "function", "function": {
        "name": ANALYSIS_TOOL_NAME,
        "description": "Record concise findings about already returned observations, explicitly linking IDs. May accompany the next query in the same response. This is local bookkeeping, not a database call. Final analyses can also be included in select_report_observations.",
        "parameters": {"type": "object", "properties": {"analyses": analysis_schema()},
                       "required": ["analyses"], "additionalProperties": False},
    }}
