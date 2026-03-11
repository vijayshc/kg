from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from string import Formatter
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from .constants import (
    VALID_CARDINALITIES,
    VALID_DATA_TYPES,
    VALID_INDEX_MAPPINGS,
    VALID_MULTIPLICITIES,
    VALID_QUERY_OPERATORS,
    VALID_RELATION_INDEX_DIRECTIONS,
    VALID_SORT_ORDERS,
    XSD_TO_JANUSGRAPH,
)

LOG = logging.getLogger("kg_loader")


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "t", "y", "yes", "on"}


def setup_logging(level: str) -> None:
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
    )


def load_optional_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    load_dotenv(override=False)


def substitute_env_values(text: str) -> str:
    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

    def replacer(match: re.Match[str]) -> str:
        var_name = match.group(1)
        default = match.group(2)
        if var_name in os.environ and os.environ[var_name] != "":
            return os.environ[var_name]
        if default is not None:
            return default
        return match.group(0)

    return pattern.sub(replacer, text)


def extract_template_fields(template: str) -> List[str]:
    return [field_name for _, field_name, _, _ in Formatter().parse(template) if field_name]


def validate_gremlin_identifier(name: str, context: str) -> None:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"{context} '{name}' must be a valid Gremlin identifier")


def validate_websocket_url(url: str) -> None:
    if not re.fullmatch(r"wss?://.+", url):
        raise ValueError(f"JanusGraph URL '{url}' must use ws:// or wss://")


def validate_sql_fragment(fragment: str, context: str) -> None:
    if any(token in fragment for token in (";", "--", "/*", "*/")):
        raise ValueError(f"Unsafe SQL tokens are not allowed in {context}: {fragment!r}")


def validate_sql_statement(statement: str, context: str) -> None:
    sql = statement.strip()
    if not sql:
        raise ValueError(f"{context} cannot be empty")
    validate_sql_fragment(sql, context)
    if not re.match(r"^(SELECT|WITH)\b", sql, flags=re.IGNORECASE):
        raise ValueError(f"{context} must be a single SELECT or WITH statement")


def split_qualified_identifier(value: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if value is None:
        return None, None
    candidate = value.strip()
    if not candidate:
        return None, candidate
    if "," in candidate:
        prefix, leaf = candidate.rsplit(",", 1)
        prefix = prefix.strip()
        leaf = leaf.strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)*", prefix) and re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_$]*", leaf
        ):
            return prefix, leaf
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)+", candidate):
        return None, candidate
    prefix, leaf = candidate.rsplit(".", 1)
    return prefix, leaf


def local_name(iri: str) -> str:
    if "#" in iri:
        return iri.rsplit("#", 1)[1]
    if "/" in iri:
        return iri.rstrip("/").rsplit("/", 1)[1]
    if ":" in iri:
        return iri.rsplit(":", 1)[1]
    return iri


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_]", "_", value)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        cleaned = "unnamed"
    if cleaned[0].isdigit():
        cleaned = f"_{cleaned}"
    return cleaned


def unique_preserve_order(values: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def chunked(values: Sequence[Dict[str, Any]], size: int) -> Iterator[List[Dict[str, Any]]]:
    if size <= 0:
        raise ValueError("Chunk size must be greater than zero")
    for index in range(0, len(values), size):
        yield list(values[index : index + size])


def normalize_row(columns: Sequence[str], values: Sequence[Any]) -> Dict[str, Any]:
    row: Dict[str, Any] = {}
    for column, value in zip(columns, values):
        row[column] = value
        row[column.lower()] = value
    return row


def render_template(template: str, row: Dict[str, Any]) -> str:
    field_names = extract_template_fields(template)
    missing = [field_name for field_name in field_names if field_name not in row]
    if missing:
        raise KeyError(
            f"Template '{template}' requires missing fields {missing}. "
            f"Available keys: {sorted(set(row.keys()))}"
        )
    return template.format(**row)


def normalize_data_type(value: Optional[str]) -> str:
    if not value:
        return "String"
    candidate = value.strip()
    match = next((item for item in VALID_DATA_TYPES if item.lower() == candidate.lower()), None)
    if not match:
        raise ValueError(f"Unsupported data_type '{value}'. Supported: {sorted(VALID_DATA_TYPES)}")
    return match


def normalize_cardinality(value: Optional[str]) -> str:
    if not value:
        return "SINGLE"
    candidate = value.strip().upper()
    if candidate not in VALID_CARDINALITIES:
        raise ValueError(f"Unsupported cardinality '{value}'. Supported: {sorted(VALID_CARDINALITIES)}")
    return candidate


def normalize_multiplicity(value: Optional[str]) -> str:
    if not value:
        return "MULTI"
    candidate = value.strip().upper()
    if candidate not in VALID_MULTIPLICITIES:
        raise ValueError(f"Unsupported multiplicity '{value}'. Supported: {sorted(VALID_MULTIPLICITIES)}")
    return candidate


def normalize_index_mapping(value: Optional[str]) -> Optional[str]:
    if value is None or str(value).strip() == "":
        return None
    candidate = str(value).strip().upper()
    if candidate not in VALID_INDEX_MAPPINGS:
        raise ValueError(f"Unsupported index mapping '{value}'. Supported: {sorted(VALID_INDEX_MAPPINGS)}")
    return candidate


def normalize_relation_direction(value: Optional[str]) -> str:
    if not value:
        return "BOTH"
    candidate = str(value).strip().upper()
    if candidate not in VALID_RELATION_INDEX_DIRECTIONS:
        raise ValueError(
            f"Unsupported relation index direction '{value}'. Supported: {sorted(VALID_RELATION_INDEX_DIRECTIONS)}"
        )
    return candidate


def normalize_sort_order(value: Optional[str]) -> str:
    if not value:
        return "asc"
    candidate = str(value).strip().lower()
    if candidate not in VALID_SORT_ORDERS:
        raise ValueError(f"Unsupported sort order '{value}'. Supported: {sorted(VALID_SORT_ORDERS)}")
    return candidate


def normalize_query_operator(value: Optional[str]) -> str:
    if not value:
        return "eq"
    candidate = str(value).strip().lower()
    if candidate not in VALID_QUERY_OPERATORS:
        raise ValueError(f"Unsupported query operator '{value}'. Supported: {sorted(VALID_QUERY_OPERATORS)}")
    return candidate


def supports_auto_graph_index(cardinality: Optional[str]) -> bool:
    return normalize_cardinality(cardinality) != "LIST"


def supports_relation_index_data_type(data_type: Optional[str]) -> bool:
    normalized_type = normalize_data_type(data_type)
    return normalized_type in {"String", "Integer", "Long", "Float", "Double", "Decimal", "Boolean", "Date"}


def default_relation_index_sort_order(data_type: Optional[str]) -> str:
    normalized_type = normalize_data_type(data_type)
    if normalized_type in {"Date"}:
        return "desc"
    return "asc"


def to_iso_instant(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def coerce_value_for_transport(value: Any, data_type: str) -> Any:
    if value is None:
        return None

    normalized_type = normalize_data_type(data_type)
    try:
        if normalized_type == "Date":
            if isinstance(value, datetime):
                return value.date().isoformat()
            if isinstance(value, date):
                return value.isoformat()
            value_str = str(value).strip()
            if "T" in value_str:
                return datetime.fromisoformat(value_str.replace("Z", "+00:00")).date().isoformat()
            return date.fromisoformat(value_str).isoformat()

        if normalized_type == "Instant":
            if isinstance(value, datetime):
                return to_iso_instant(value)
            if isinstance(value, date):
                return f"{value.isoformat()}T00:00:00Z"
            value_str = str(value).strip()
            normalized = value_str
            if "T" not in normalized:
                normalized = f"{normalized}T00:00:00Z"
            elif not normalized.endswith("Z") and "+" not in normalized[10:]:
                normalized = f"{normalized}Z"
            datetime.fromisoformat(normalized.replace("Z", "+00:00"))
            return normalized

        if normalized_type in {"Integer", "Long"}:
            return int(value)

        if normalized_type in {"Float", "Double"}:
            return float(value)

        if normalized_type == "Boolean":
            return value if isinstance(value, bool) else as_bool(value)

        if normalized_type == "UUID":
            return str(uuid.UUID(str(value)))

        if normalized_type == "Decimal":
            return float(Decimal(str(value)))

        if isinstance(value, (datetime, date, Decimal, uuid.UUID)):
            return str(value)
        return value
    except Exception as exc:
        raise ValueError(f"Cannot coerce value {value!r} to JanusGraph type {normalized_type}") from exc


def infer_data_type_from_ranges(ranges: Iterable[str]) -> str:
    for iri in ranges:
        if iri in XSD_TO_JANUSGRAPH:
            return XSD_TO_JANUSGRAPH[iri]
    return "String"


def encode_sorted_string_set(values: Iterable[str]) -> str:
    return json.dumps(sorted(set(values)), separators=(",", ":"))


def multiplicity_supports_outgoing_functional(multiplicity: str) -> bool:
    normalized = normalize_multiplicity(multiplicity)
    return normalized in {"MANY2ONE", "ONE2ONE"}


def multiplicity_supports_incoming_functional(multiplicity: str) -> bool:
    normalized = normalize_multiplicity(multiplicity)
    return normalized in {"ONE2MANY", "ONE2ONE"}
