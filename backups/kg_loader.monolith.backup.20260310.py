#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import ssl
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from string import Formatter
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple


LOG = logging.getLogger("kg_loader")

VALID_DATA_TYPES = {
    "String",
    "Integer",
    "Long",
    "Float",
    "Double",
    "Decimal",
    "Boolean",
    "Date",
    "Instant",
    "UUID",
}
VALID_CARDINALITIES = {"SINGLE", "SET", "LIST"}
VALID_MULTIPLICITIES = {"MULTI", "SIMPLE", "MANY2ONE", "ONE2MANY", "ONE2ONE"}
VALID_MODES = {"test", "prod"}
VALID_SOURCE_TYPES = {"csv", "teradata"}

XSD_TO_JANUSGRAPH = {
    "http://www.w3.org/2001/XMLSchema#string": "String",
    "http://www.w3.org/2001/XMLSchema#normalizedString": "String",
    "http://www.w3.org/2001/XMLSchema#token": "String",
    "http://www.w3.org/2001/XMLSchema#int": "Integer",
    "http://www.w3.org/2001/XMLSchema#integer": "Long",
    "http://www.w3.org/2001/XMLSchema#long": "Long",
    "http://www.w3.org/2001/XMLSchema#float": "Float",
    "http://www.w3.org/2001/XMLSchema#double": "Double",
    "http://www.w3.org/2001/XMLSchema#decimal": "Decimal",
    "http://www.w3.org/2001/XMLSchema#boolean": "Boolean",
    "http://www.w3.org/2001/XMLSchema#date": "Date",
    "http://www.w3.org/2001/XMLSchema#dateTime": "Instant",
    "http://www.w3.org/2001/XMLSchema#dateTimeStamp": "Instant",
}

SYSTEM_VERTEX_PROPERTY_SPECS = (
    {"name": "external_id", "data_type": "String", "cardinality": "SINGLE"},
    {"name": "ontology_iri", "data_type": "String", "cardinality": "SINGLE"},
    {"name": "rdf_types", "data_type": "String", "cardinality": "SET"},
    {"name": "source_table", "data_type": "String", "cardinality": "SINGLE"},
    {"name": "source_primary_key", "data_type": "String", "cardinality": "SINGLE"},
    {"name": "load_batch_id", "data_type": "String", "cardinality": "SINGLE"},
    {"name": "loaded_at", "data_type": "Instant", "cardinality": "SINGLE"},
)
SYSTEM_EDGE_PROPERTY_SPECS = (
    {"name": "edge_external_id", "data_type": "String", "cardinality": "SINGLE"},
    {"name": "ontology_iri", "data_type": "String", "cardinality": "SINGLE"},
    {"name": "ontology_property_lineage", "data_type": "String", "cardinality": "SINGLE"},
    {"name": "ontology_inverse_property_iris", "data_type": "String", "cardinality": "SINGLE"},
    {"name": "source_table", "data_type": "String", "cardinality": "SINGLE"},
    {"name": "load_batch_id", "data_type": "String", "cardinality": "SINGLE"},
    {"name": "loaded_at", "data_type": "Instant", "cardinality": "SINGLE"},
)

MANAGEMENT_SCRIPT_TEMPLATE = r'''
import org.apache.tinkerpop.gremlin.structure.Edge
import org.apache.tinkerpop.gremlin.structure.Vertex
import org.janusgraph.core.Cardinality
import org.janusgraph.core.Multiplicity

def resolveClass = { typeName ->
  switch ((typeName ?: 'String').toUpperCase()) {
    case 'INTEGER': return Integer.class
    case 'LONG': return Long.class
    case 'FLOAT': return Float.class
    case 'DOUBLE': return Double.class
    case 'DECIMAL': return Double.class
    case 'BOOLEAN': return Boolean.class
    case 'DATE': return java.util.Date.class
    case 'INSTANT': return java.time.Instant.class
    case 'UUID': return java.util.UUID.class
    default: return String.class
  }
}

def resolveCardinality = { value ->
  switch ((value ?: 'SINGLE').toUpperCase()) {
    case 'SET': return Cardinality.SET
    case 'LIST': return Cardinality.LIST
    default: return Cardinality.SINGLE
  }
}

def resolveMultiplicity = { value ->
  switch ((value ?: 'MULTI').toUpperCase()) {
    case 'SIMPLE': return Multiplicity.SIMPLE
    case 'MANY2ONE': return Multiplicity.MANY2ONE
    case 'ONE2MANY': return Multiplicity.ONE2MANY
    case 'ONE2ONE': return Multiplicity.ONE2ONE
    default: return Multiplicity.MULTI
  }
}

mgmt = __GRAPH_ALIAS__.openManagement()
try {
  schema.property_keys.each { spec ->
    if (mgmt.getPropertyKey(spec.name) == null) {
      mgmt.makePropertyKey(spec.name)
          .dataType(resolveClass(spec.data_type))
          .cardinality(resolveCardinality(spec.cardinality))
          .make()
    }
  }

  schema.vertex_labels.each { spec ->
    if (mgmt.getVertexLabel(spec.name) == null) {
      mgmt.makeVertexLabel(spec.name).make()
    }
  }

  schema.edge_labels.each { spec ->
    if (mgmt.getEdgeLabel(spec.name) == null) {
      mgmt.makeEdgeLabel(spec.name)
          .multiplicity(resolveMultiplicity(spec.multiplicity))
          .make()
    }
  }

  schema.vertex_indexes.each { spec ->
    if (mgmt.getGraphIndex(spec.name) == null) {
      def key = mgmt.getPropertyKey(spec.property_key)
      def builder = mgmt.buildIndex(spec.name, Vertex.class).addKey(key)
      if (spec.unique) {
        builder.unique()
      }
      builder.buildCompositeIndex()
    }
  }

  schema.edge_indexes.each { spec ->
    if (mgmt.getGraphIndex(spec.name) == null) {
      def key = mgmt.getPropertyKey(spec.property_key)
      def builder = mgmt.buildIndex(spec.name, Edge.class).addKey(key)
      if (spec.unique) {
        builder.unique()
      }
      builder.buildCompositeIndex()
    }
  }

  mgmt.commit()
} catch (Throwable t) {
  try {
    mgmt.rollback()
  } catch (Throwable ignored) {
  }
  throw t
}
return 'schema-ok'
'''

VERTEX_BATCH_SCRIPT = r'''
import org.apache.tinkerpop.gremlin.structure.VertexProperty

def coerce = { value, typeName ->
  if (value == null) {
    return null
  }
  switch ((typeName ?: 'String').toUpperCase()) {
    case 'INTEGER': return value instanceof Integer ? value : Integer.valueOf(value.toString())
    case 'LONG': return value instanceof Long ? value : Long.valueOf(value.toString())
    case 'FLOAT': return value instanceof Float ? value : Float.valueOf(value.toString())
    case 'DOUBLE': return value instanceof Double ? value : Double.valueOf(value.toString())
    case 'DECIMAL': return value instanceof Double ? value : Double.valueOf(value.toString())
    case 'BOOLEAN': return value instanceof Boolean ? value : value.toString().toBoolean()
    case 'DATE': return value instanceof java.util.Date ? value : java.util.Date.from(java.time.LocalDate.parse(value.toString()).atStartOfDay(java.time.ZoneOffset.UTC).toInstant())
    case 'INSTANT': return value instanceof java.time.Instant ? value : java.time.Instant.parse(value.toString())
    case 'UUID': return value instanceof java.util.UUID ? value : java.util.UUID.fromString(value.toString())
    default: return value.toString()
  }
}

def cardinality = { value ->
  switch ((value ?: 'SINGLE').toUpperCase()) {
    case 'SET': return VertexProperty.Cardinality.set
    case 'LIST': return VertexProperty.Cardinality.list
    default: return VertexProperty.Cardinality.single
  }
}

def processed = 0
rows.each { row ->
    def vertex = g.V().hasLabel(vertexLabel).has(idKey, row.external_id).fold()
            .coalesce(unfold(), addV(vertexLabel).property(VertexProperty.Cardinality.single, idKey, row.external_id))
      .next()

  row.properties.each { prop ->
    if (prop.value != null) {
      vertex.property(cardinality(prop.cardinality), prop.key, coerce(prop.value, prop.data_type))
    }
  }
  processed++
}
return processed
'''

VERTEX_PROPERTY_UPDATE_SCRIPT = r'''
import org.apache.tinkerpop.gremlin.structure.VertexProperty

def coerce = { value, typeName ->
  if (value == null) {
    return null
  }
  switch ((typeName ?: 'String').toUpperCase()) {
    case 'INTEGER': return value instanceof Integer ? value : Integer.valueOf(value.toString())
    case 'LONG': return value instanceof Long ? value : Long.valueOf(value.toString())
    case 'FLOAT': return value instanceof Float ? value : Float.valueOf(value.toString())
    case 'DOUBLE': return value instanceof Double ? value : Double.valueOf(value.toString())
    case 'DECIMAL': return value instanceof Double ? value : Double.valueOf(value.toString())
    case 'BOOLEAN': return value instanceof Boolean ? value : value.toString().toBoolean()
    case 'DATE': return value instanceof java.util.Date ? value : java.util.Date.from(java.time.LocalDate.parse(value.toString()).atStartOfDay(java.time.ZoneOffset.UTC).toInstant())
    case 'INSTANT': return value instanceof java.time.Instant ? value : java.time.Instant.parse(value.toString())
    case 'UUID': return value instanceof java.util.UUID ? value : java.util.UUID.fromString(value.toString())
    default: return value.toString()
  }
}

def cardinality = { value ->
  switch ((value ?: 'SINGLE').toUpperCase()) {
    case 'SET': return VertexProperty.Cardinality.set
    case 'LIST': return VertexProperty.Cardinality.list
    default: return VertexProperty.Cardinality.single
  }
}

def processed = 0
def missing = 0
rows.each { row ->
    def vertex = g.V().hasLabel(vertexLabel).has(idKey, row.external_id).tryNext().orElse(null)
  if (vertex == null) {
    missing++
    return
  }

  row.properties.each { prop ->
    if (prop.value != null) {
      vertex.property(cardinality(prop.cardinality), prop.key, coerce(prop.value, prop.data_type))
    }
  }
  processed++
}
return [processed: processed, missing: missing]
'''

EDGE_BATCH_SCRIPT = r'''
def coerce = { value, typeName ->
  if (value == null) {
    return null
  }
  switch ((typeName ?: 'String').toUpperCase()) {
    case 'INTEGER': return value instanceof Integer ? value : Integer.valueOf(value.toString())
    case 'LONG': return value instanceof Long ? value : Long.valueOf(value.toString())
    case 'FLOAT': return value instanceof Float ? value : Float.valueOf(value.toString())
    case 'DOUBLE': return value instanceof Double ? value : Double.valueOf(value.toString())
    case 'DECIMAL': return value instanceof Double ? value : Double.valueOf(value.toString())
    case 'BOOLEAN': return value instanceof Boolean ? value : value.toString().toBoolean()
    case 'DATE': return value instanceof java.util.Date ? value : java.util.Date.from(java.time.LocalDate.parse(value.toString()).atStartOfDay(java.time.ZoneOffset.UTC).toInstant())
    case 'INSTANT': return value instanceof java.time.Instant ? value : java.time.Instant.parse(value.toString())
    case 'UUID': return value instanceof java.util.UUID ? value : java.util.UUID.fromString(value.toString())
    default: return value.toString()
  }
}

def processed = 0
def missing = 0
rows.each { row ->
  def outV = g.V().hasLabel(outLabel).has(idKey, row.out_external_id).tryNext().orElse(null)
  def inV = g.V().hasLabel(inLabel).has(idKey, row.in_external_id).tryNext().orElse(null)

  if (outV == null || inV == null) {
    missing++
    return
  }

  def edge = g.E().hasLabel(edgeLabel).has(edgeIdKey, row.edge_external_id).tryNext().orElse(null)
  if (edge == null) {
    edge = outV.addEdge(edgeLabel, inV, edgeIdKey, row.edge_external_id)
  }

  row.properties.each { prop ->
    if (prop.value != null) {
      edge.property(prop.key, coerce(prop.value, prop.data_type))
    }
  }

  processed++
}
return [processed: processed, missing: missing]
'''


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


def build_management_script(graph_alias: str) -> str:
    validate_gremlin_identifier(graph_alias, "JanusGraph graph alias")
    return MANAGEMENT_SCRIPT_TEMPLATE.replace("__GRAPH_ALIAS__", graph_alias)


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
            return date.fromisoformat(str(value)).isoformat()

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


@dataclass
class OntologyDefinition:
    file: str
    format: Optional[str] = None
    include_unmapped_terms: bool = True


@dataclass
class SourceSpec:
    table: Optional[str] = None
    sql: Optional[str] = None
    where: Optional[str] = None
    fetch_size: int = 1000
    key_column: Optional[str] = None
    from_column: Optional[str] = None
    to_column: Optional[str] = None

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "SourceSpec":
        raw = raw or {}
        return cls(
            table=raw.get("table"),
            sql=raw.get("sql"),
            where=raw.get("where"),
            fetch_size=int(raw.get("fetch_size", 1000)),
            key_column=raw.get("key_column"),
            from_column=raw.get("from_column"),
            to_column=raw.get("to_column"),
        )


@dataclass
class PropertyMapping:
    iri: str
    property_key: Optional[str] = None
    source_column: Optional[str] = None
    source_table: Optional[str] = None
    source_sql: Optional[str] = None
    entity_key_column: Optional[str] = None
    where: Optional[str] = None
    data_type: Optional[str] = None
    cardinality: str = "SINGLE"

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "PropertyMapping":
        return cls(
            iri=raw["iri"],
            property_key=raw.get("property_key"),
            source_column=raw.get("source_column"),
            source_table=raw.get("source_table"),
            source_sql=raw.get("source_sql"),
            entity_key_column=raw.get("entity_key_column"),
            where=raw.get("where"),
            data_type=raw.get("data_type"),
            cardinality=normalize_cardinality(raw.get("cardinality")),
        )

    def resolved_property_key(self) -> str:
        return self.property_key or safe_name(local_name(self.iri))


@dataclass
class ClassMapping:
    iri: str
    vertex_label: Optional[str] = None
    id_template: Optional[str] = None
    source: SourceSpec = field(default_factory=SourceSpec)
    properties: List[PropertyMapping] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "ClassMapping":
        return cls(
            iri=raw["iri"],
            vertex_label=raw.get("vertex_label"),
            id_template=raw.get("id_template"),
            source=SourceSpec.from_dict(raw.get("source")),
            properties=[PropertyMapping.from_dict(item) for item in raw.get("properties", [])],
        )

    def resolved_vertex_label(self) -> str:
        return self.vertex_label or safe_name(local_name(self.iri))

    def resolved_id_template(self) -> str:
        if self.id_template:
            return self.id_template
        if not self.source.key_column:
            raise ValueError(f"Class '{self.iri}' requires source.key_column for default id template")
        return f"{self.resolved_vertex_label()}:{{{self.source.key_column}}}"


@dataclass
class RelationshipMapping:
    iri: str
    source_class_iri: str
    target_class_iri: str
    edge_label: Optional[str] = None
    id_template: Optional[str] = None
    multiplicity: str = "MULTI"
    source: SourceSpec = field(default_factory=SourceSpec)
    properties: List[PropertyMapping] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "RelationshipMapping":
        return cls(
            iri=raw["iri"],
            source_class_iri=raw["source_class_iri"],
            target_class_iri=raw["target_class_iri"],
            edge_label=raw.get("edge_label"),
            id_template=raw.get("id_template"),
            multiplicity=normalize_multiplicity(raw.get("multiplicity")),
            source=SourceSpec.from_dict(raw.get("source")),
            properties=[PropertyMapping.from_dict(item) for item in raw.get("properties", [])],
        )

    def resolved_edge_label(self) -> str:
        return self.edge_label or safe_name(local_name(self.iri))

    def resolved_id_template(self, source_class: ClassMapping, target_class: ClassMapping) -> str:
        if self.id_template:
            return self.id_template
        if not source_class.source.key_column or not target_class.source.key_column:
            raise ValueError(f"Relationship '{self.iri}' requires source/target class key columns")
        return (
            f"{self.resolved_edge_label()}:"
            f"{{{source_class.source.key_column}}}:{{{target_class.source.key_column}}}"
        )


@dataclass
class JanusGraphSettings:
    url: str
    traversal_source: str = "g"
    graph_alias: str = "graph"
    create_schema: bool = True
    request_timeout_seconds: int = 300


@dataclass
class RuntimeSettings:
    batch_size: int = 500
    dry_run: bool = False
    strict_ontology: bool = True
    fail_on_missing_vertex: bool = False


@dataclass
class IngestionSettings:
    mode: str = "prod"
    source_type: str = "teradata"
    csv_root_dir: Optional[str] = None
    csv_delimiter: str = ","
    csv_encoding: str = "utf-8"


@dataclass
class LoaderConfig:
    ontology: OntologyDefinition
    janusgraph: JanusGraphSettings
    runtime: RuntimeSettings
    ingestion: IngestionSettings
    classes: List[ClassMapping]
    relationships: List[RelationshipMapping]
    mapping_file: str

    @classmethod
    def from_file(
        cls,
        file_path: str,
        ontology_override: Optional[str] = None,
        dry_run_override: Optional[bool] = None,
        strict_override: Optional[bool] = None,
    ) -> "LoaderConfig":
        config_path = Path(file_path).resolve()
        raw = load_structured_file(config_path)

        ontology_raw = raw.get("ontology", {})
        janus_raw = raw.get("janusgraph", {})
        runtime_raw = raw.get("runtime", {})
        ingestion_raw = raw.get("ingestion", {})

        ontology_file = ontology_override or ontology_raw.get("file")
        if not ontology_file:
            raise ValueError("Ontology file must be set in the mapping file or via --ontology")

        ontology_path = Path(ontology_file)
        if not ontology_path.is_absolute():
            ontology_path = (config_path.parent / ontology_path).resolve()

        csv_root_dir = ingestion_raw.get("csv_root_dir")
        if csv_root_dir:
            csv_root_path = Path(str(csv_root_dir))
            if not csv_root_path.is_absolute():
                csv_root_path = (config_path.parent / csv_root_path).resolve()
            csv_root_dir = str(csv_root_path)

        config = cls(
            ontology=OntologyDefinition(
                file=str(ontology_path),
                format=ontology_raw.get("format"),
                include_unmapped_terms=as_bool(ontology_raw.get("include_unmapped_terms", True), True),
            ),
            janusgraph=JanusGraphSettings(
                url=str(janus_raw.get("url") or os.getenv("JANUSGRAPH_URL", "")),
                traversal_source=str(janus_raw.get("traversal_source") or os.getenv("JANUSGRAPH_TRAVERSAL_SOURCE", "g")),
                graph_alias=str(janus_raw.get("graph_alias") or os.getenv("JANUSGRAPH_GRAPH_ALIAS", "graph")),
                create_schema=as_bool(janus_raw.get("create_schema", True), True),
                request_timeout_seconds=int(
                    janus_raw.get("request_timeout_seconds")
                    or os.getenv("JANUSGRAPH_REQUEST_TIMEOUT_SECONDS", 300)
                ),
            ),
            runtime=RuntimeSettings(
                batch_size=int(runtime_raw.get("batch_size") or os.getenv("KG_BATCH_SIZE", 500)),
                dry_run=(
                    dry_run_override
                    if dry_run_override is not None
                    else as_bool(runtime_raw.get("dry_run"), False)
                ),
                strict_ontology=(
                    strict_override
                    if strict_override is not None
                    else as_bool(runtime_raw.get("strict_ontology"), as_bool(os.getenv("KG_STRICT_ONTOLOGY"), True))
                ),
                fail_on_missing_vertex=as_bool(runtime_raw.get("fail_on_missing_vertex"), False),
            ),
            ingestion=IngestionSettings(
                mode=str(ingestion_raw.get("mode", "prod")).lower(),
                source_type=str(ingestion_raw.get("source_type", "teradata")).lower(),
                csv_root_dir=csv_root_dir,
                csv_delimiter=str(ingestion_raw.get("csv_delimiter", ",")),
                csv_encoding=str(ingestion_raw.get("csv_encoding", "utf-8")),
            ),
            classes=[ClassMapping.from_dict(item) for item in raw.get("classes", [])],
            relationships=[RelationshipMapping.from_dict(item) for item in raw.get("relationships", [])],
            mapping_file=str(config_path),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not Path(self.ontology.file).exists():
            raise FileNotFoundError(f"Ontology file not found: {self.ontology.file}")

        if self.ingestion.mode not in VALID_MODES:
            raise ValueError(f"Unsupported ingestion mode '{self.ingestion.mode}'. Supported: {sorted(VALID_MODES)}")
        if self.ingestion.source_type not in VALID_SOURCE_TYPES:
            raise ValueError(
                f"Unsupported ingestion source_type '{self.ingestion.source_type}'. Supported: {sorted(VALID_SOURCE_TYPES)}"
            )
        if self.ingestion.source_type == "csv":
            if not self.ingestion.csv_root_dir:
                raise ValueError("CSV ingestion requires ingestion.csv_root_dir")
            if not Path(self.ingestion.csv_root_dir).exists():
                raise FileNotFoundError(f"CSV root directory not found: {self.ingestion.csv_root_dir}")

        validate_gremlin_identifier(self.janusgraph.traversal_source, "JanusGraph traversal source")
        validate_gremlin_identifier(self.janusgraph.graph_alias, "JanusGraph graph alias")
        if not self.runtime.dry_run:
            validate_websocket_url(self.janusgraph.url)

        class_iris = set()
        vertex_labels = set()
        for class_mapping in self.classes:
            if class_mapping.iri in class_iris:
                raise ValueError(f"Duplicate class mapping for IRI '{class_mapping.iri}'")
            class_iris.add(class_mapping.iri)

            vertex_label = class_mapping.resolved_vertex_label()
            if vertex_label in vertex_labels:
                raise ValueError(f"Duplicate vertex label '{vertex_label}'")
            vertex_labels.add(vertex_label)

            if not class_mapping.source.table and not class_mapping.source.sql:
                raise ValueError(f"Class '{class_mapping.iri}' requires source.table or source.sql")
            if not class_mapping.source.key_column:
                raise ValueError(f"Class '{class_mapping.iri}' requires source.key_column")

            self._validate_source_spec(class_mapping.source, f"Class '{class_mapping.iri}' source")

            for prop in class_mapping.properties:
                if not prop.source_column:
                    raise ValueError(f"Property '{prop.iri}' requires source_column")
                validate_sql_fragment(prop.source_column, f"Property '{prop.iri}' source_column")
                if prop.source_table:
                    validate_sql_fragment(prop.source_table, f"Property '{prop.iri}' source_table")
                if prop.source_sql:
                    validate_sql_statement(prop.source_sql, f"Property '{prop.iri}' source_sql")
                    if self.ingestion.source_type == "csv":
                        raise ValueError(f"Property '{prop.iri}' cannot use source_sql in CSV ingestion mode")
                if prop.entity_key_column:
                    validate_sql_fragment(prop.entity_key_column, f"Property '{prop.iri}' entity_key_column")
                if prop.where:
                    validate_sql_fragment(prop.where, f"Property '{prop.iri}' where clause")
                    if self.ingestion.source_type == "csv":
                        raise ValueError(f"Property '{prop.iri}' cannot use where in CSV ingestion mode")

            inline_props, external_props = split_class_properties(class_mapping)
            if not class_mapping.source.sql:
                available_fields = {class_mapping.source.key_column}
                available_fields.update(prop.source_column for prop in inline_props if prop.source_column)
                self._validate_template_fields(
                    class_mapping.resolved_id_template(),
                    available_fields,
                    f"Class '{class_mapping.iri}' base query template",
                )

            for prop in external_props:
                if not prop.source_sql:
                    available_fields = {class_mapping.source.key_column}
                    if prop.source_column:
                        available_fields.add(prop.source_column)
                    self._validate_template_fields(
                        class_mapping.resolved_id_template(),
                        available_fields,
                        f"Class '{class_mapping.iri}' external property '{prop.iri}' template",
                    )

        relationship_labels = set()
        classes_by_iri = {item.iri: item for item in self.classes}
        for relationship in self.relationships:
            if relationship.source_class_iri not in classes_by_iri:
                raise ValueError(
                    f"Relationship '{relationship.iri}' references unmapped source class "
                    f"'{relationship.source_class_iri}'"
                )
            if relationship.target_class_iri not in classes_by_iri:
                raise ValueError(
                    f"Relationship '{relationship.iri}' references unmapped target class "
                    f"'{relationship.target_class_iri}'"
                )
            if not relationship.source.table and not relationship.source.sql:
                raise ValueError(f"Relationship '{relationship.iri}' requires source.table or source.sql")
            if not relationship.source.from_column or not relationship.source.to_column:
                raise ValueError(f"Relationship '{relationship.iri}' requires source.from_column and source.to_column")

            self._validate_source_spec(relationship.source, f"Relationship '{relationship.iri}' source")

            edge_label = relationship.resolved_edge_label()
            if edge_label in relationship_labels:
                raise ValueError(f"Duplicate edge label '{edge_label}'")
            relationship_labels.add(edge_label)

            for prop in relationship.properties:
                if not prop.source_column:
                    raise ValueError(f"Relationship property '{prop.iri}' requires source_column")
                validate_sql_fragment(prop.source_column, f"Relationship property '{prop.iri}' source_column")

            if not relationship.source.sql:
                source_class = classes_by_iri[relationship.source_class_iri]
                target_class = classes_by_iri[relationship.target_class_iri]
                available_fields = {
                    source_class.source.key_column,
                    target_class.source.key_column,
                }
                available_fields.update(prop.source_column for prop in relationship.properties if prop.source_column)
                self._validate_template_fields(
                    source_class.resolved_id_template(),
                    available_fields,
                    f"Relationship '{relationship.iri}' source vertex template",
                )
                self._validate_template_fields(
                    target_class.resolved_id_template(),
                    available_fields,
                    f"Relationship '{relationship.iri}' target vertex template",
                )
                self._validate_template_fields(
                    relationship.resolved_id_template(source_class, target_class),
                    available_fields,
                    f"Relationship '{relationship.iri}' edge template",
                )

    def _validate_source_spec(self, source: SourceSpec, context: str) -> None:
        if source.table:
            validate_sql_fragment(source.table, f"{context} table")
        if source.sql:
            validate_sql_statement(source.sql, f"{context} sql")
            if self.ingestion.source_type == "csv":
                raise ValueError(f"{context} cannot use sql in CSV ingestion mode")
        if source.where:
            validate_sql_fragment(source.where, f"{context} where clause")
            if self.ingestion.source_type == "csv":
                raise ValueError(f"{context} cannot use where in CSV ingestion mode")
        if source.key_column:
            validate_sql_fragment(source.key_column, f"{context} key_column")
        if source.from_column:
            validate_sql_fragment(source.from_column, f"{context} from_column")
        if source.to_column:
            validate_sql_fragment(source.to_column, f"{context} to_column")

    @staticmethod
    def _validate_template_fields(template: str, available_fields: set[str], context: str) -> None:
        missing = sorted(set(extract_template_fields(template)) - set(available_fields))
        if missing:
            raise ValueError(
                f"{context} references placeholder(s) {missing} that are not available in the generated query. "
                f"Available fields: {sorted(set(available_fields))}"
            )


@dataclass
class OntologyClass:
    iri: str
    label: Optional[str] = None
    comment: Optional[str] = None
    superclasses: set[str] = field(default_factory=set)
    equivalent_classes: set[str] = field(default_factory=set)
    disjoint_classes: set[str] = field(default_factory=set)


@dataclass
class OntologyRestriction:
    source_class_iri: str
    property_iri: str
    constraint_type: str
    cardinality: Optional[int] = None
    filler_iri: Optional[str] = None
    filler_data_type: Optional[str] = None
    has_value: Optional[Any] = None


@dataclass
class OntologyProperty:
    iri: str
    kind: str
    label: Optional[str] = None
    comment: Optional[str] = None
    domains: set[str] = field(default_factory=set)
    ranges: set[str] = field(default_factory=set)
    superproperties: set[str] = field(default_factory=set)
    equivalent_properties: set[str] = field(default_factory=set)
    inverse_properties: set[str] = field(default_factory=set)
    characteristics: set[str] = field(default_factory=set)


@dataclass
class OntologyCatalog:
    classes: Dict[str, OntologyClass] = field(default_factory=dict)
    properties: Dict[str, OntologyProperty] = field(default_factory=dict)
    restrictions_by_class: Dict[str, List[OntologyRestriction]] = field(default_factory=dict)

    def ensure_class(self, iri: str) -> OntologyClass:
        if iri not in self.classes:
            self.classes[iri] = OntologyClass(iri=iri)
        return self.classes[iri]

    def ensure_property(self, iri: str, kind: str = "unknown") -> OntologyProperty:
        if iri not in self.properties:
            self.properties[iri] = OntologyProperty(iri=iri, kind=kind)
        elif self.properties[iri].kind == "unknown" and kind != "unknown":
            self.properties[iri].kind = kind
        return self.properties[iri]

    def add_restriction(self, restriction: OntologyRestriction) -> None:
        self.restrictions_by_class.setdefault(restriction.source_class_iri, []).append(restriction)

    def class_lineage(self, iri: str) -> set[str]:
        seen: set[str] = set()
        stack: List[str] = [iri]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            ontology_class = self.classes.get(current)
            if not ontology_class:
                continue
            stack.extend(ontology_class.superclasses)
            stack.extend(ontology_class.equivalent_classes)
        return seen

    def class_is_assignable_to(self, actual_class_iri: str, expected_class_iri: str) -> bool:
        return expected_class_iri in self.class_lineage(actual_class_iri)

    def property_lineage(self, iri: str) -> set[str]:
        seen: set[str] = set()
        stack: List[str] = [iri]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            ontology_property = self.properties.get(current)
            if not ontology_property:
                continue
            stack.extend(ontology_property.superproperties)
            stack.extend(ontology_property.equivalent_properties)
        return seen

    def property_equivalence_group(self, iri: str) -> set[str]:
        seen: set[str] = set()
        stack: List[str] = [iri]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            ontology_property = self.properties.get(current)
            if not ontology_property:
                continue
            stack.extend(ontology_property.equivalent_properties)
        return seen

    def effective_property_domains(self, iri: str) -> set[str]:
        domains: set[str] = set()
        for property_iri in self.property_lineage(iri):
            ontology_property = self.properties.get(property_iri)
            if ontology_property:
                domains.update(ontology_property.domains)
        return domains

    def effective_property_ranges(self, iri: str) -> set[str]:
        ranges: set[str] = set()
        for property_iri in self.property_lineage(iri):
            ontology_property = self.properties.get(property_iri)
            if ontology_property:
                ranges.update(ontology_property.ranges)
        return ranges

    def effective_property_characteristics(self, iri: str) -> set[str]:
        characteristics: set[str] = set()
        for property_iri in self.property_lineage(iri):
            ontology_property = self.properties.get(property_iri)
            if ontology_property:
                characteristics.update(ontology_property.characteristics)
        return characteristics

    def inverse_properties_for(self, iri: str) -> set[str]:
        inverses: set[str] = set()
        for property_iri in self.property_equivalence_group(iri):
            ontology_property = self.properties.get(property_iri)
            if not ontology_property:
                continue
            inverses.update(ontology_property.inverse_properties)

        expanded_inverses: set[str] = set()
        for inverse_iri in inverses:
            expanded_inverses.update(self.property_equivalence_group(inverse_iri))
        return expanded_inverses

    def restrictions_for_class(self, iri: str) -> List[OntologyRestriction]:
        restrictions: List[OntologyRestriction] = []
        seen: set[Tuple[str, str, Optional[int], Optional[str], Optional[str], Optional[str]]] = set()
        for class_iri in self.class_lineage(iri):
            for restriction in self.restrictions_by_class.get(class_iri, []):
                key = (
                    restriction.property_iri,
                    restriction.constraint_type,
                    restriction.cardinality,
                    restriction.filler_iri,
                    restriction.filler_data_type,
                    json.dumps(restriction.has_value, sort_keys=True) if isinstance(restriction.has_value, dict) else str(restriction.has_value),
                )
                if key in seen:
                    continue
                seen.add(key)
                restrictions.append(restriction)
        return restrictions

    def max_cardinality_for_class_property(self, class_iri: str, property_iri: str) -> Optional[int]:
        candidates: List[int] = []
        property_lineage = self.property_lineage(property_iri)
        for restriction in self.restrictions_for_class(class_iri):
            if restriction.property_iri not in property_lineage:
                continue
            if restriction.constraint_type == "max_cardinality" and restriction.cardinality is not None:
                candidates.append(restriction.cardinality)
            elif restriction.constraint_type == "exact_cardinality" and restriction.cardinality is not None:
                candidates.append(restriction.cardinality)
        return min(candidates) if candidates else None

    def min_cardinality_for_class_property(self, class_iri: str, property_iri: str) -> Optional[int]:
        candidates: List[int] = []
        property_lineage = self.property_lineage(property_iri)
        for restriction in self.restrictions_for_class(class_iri):
            if restriction.property_iri not in property_lineage:
                continue
            if restriction.constraint_type == "min_cardinality" and restriction.cardinality is not None:
                candidates.append(restriction.cardinality)
            elif restriction.constraint_type == "exact_cardinality" and restriction.cardinality is not None:
                candidates.append(restriction.cardinality)
            elif restriction.constraint_type == "some_values_from":
                candidates.append(1)
            elif restriction.constraint_type == "has_value":
                candidates.append(1)
        return max(candidates) if candidates else None


@dataclass
class PropertyKeySpec:
    name: str
    data_type: str
    cardinality: str


@dataclass
class VertexLabelSpec:
    name: str
    ontology_iri: Optional[str] = None


@dataclass
class EdgeLabelSpec:
    name: str
    multiplicity: str
    ontology_iri: Optional[str] = None


@dataclass
class IndexSpec:
    name: str
    property_key: str
    unique: bool


@dataclass
class SchemaPlan:
    property_keys: Dict[str, PropertyKeySpec] = field(default_factory=dict)
    vertex_labels: Dict[str, VertexLabelSpec] = field(default_factory=dict)
    edge_labels: Dict[str, EdgeLabelSpec] = field(default_factory=dict)
    vertex_indexes: Dict[str, IndexSpec] = field(default_factory=dict)
    edge_indexes: Dict[str, IndexSpec] = field(default_factory=dict)

    def ensure_property_key(self, name: str, data_type: str, cardinality: str) -> None:
        existing = self.property_keys.get(name)
        spec = PropertyKeySpec(name=name, data_type=normalize_data_type(data_type), cardinality=normalize_cardinality(cardinality))
        if existing and (existing.data_type != spec.data_type or existing.cardinality != spec.cardinality):
            raise ValueError(
                f"Property key '{name}' has conflicting definitions: "
                f"existing={existing}, requested={spec}"
            )
        self.property_keys[name] = spec

    def ensure_vertex_label(self, name: str, ontology_iri: Optional[str] = None) -> None:
        self.vertex_labels[name] = VertexLabelSpec(name=name, ontology_iri=ontology_iri)

    def ensure_edge_label(self, name: str, multiplicity: str, ontology_iri: Optional[str] = None) -> None:
        existing = self.edge_labels.get(name)
        spec = EdgeLabelSpec(name=name, multiplicity=normalize_multiplicity(multiplicity), ontology_iri=ontology_iri)
        if existing and existing.multiplicity != spec.multiplicity:
            raise ValueError(
                f"Edge label '{name}' has conflicting multiplicities: "
                f"existing={existing.multiplicity}, requested={spec.multiplicity}"
            )
        self.edge_labels[name] = spec

    def ensure_vertex_index(self, name: str, property_key: str, unique: bool) -> None:
        self.vertex_indexes[name] = IndexSpec(name=name, property_key=property_key, unique=unique)

    def ensure_edge_index(self, name: str, property_key: str, unique: bool) -> None:
        self.edge_indexes[name] = IndexSpec(name=name, property_key=property_key, unique=unique)

    def as_binding(self) -> Dict[str, Any]:
        return {
            "property_keys": [asdict(item) for item in self.property_keys.values()],
            "vertex_labels": [asdict(item) for item in self.vertex_labels.values()],
            "edge_labels": [asdict(item) for item in self.edge_labels.values()],
            "vertex_indexes": [asdict(item) for item in self.vertex_indexes.values()],
            "edge_indexes": [asdict(item) for item in self.edge_indexes.values()],
        }

    def to_dict(self) -> Dict[str, Any]:
        return self.as_binding()


def load_structured_file(file_path: Path) -> Dict[str, Any]:
    raw_text = substitute_env_values(file_path.read_text(encoding="utf-8"))
    suffix = file_path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError("PyYAML is required to read YAML mapping files") from exc
        data = yaml.safe_load(raw_text) or {}
    elif suffix == ".json":
        data = json.loads(raw_text)
    else:
        raise ValueError(f"Unsupported config format '{suffix}'. Use .yaml, .yml, or .json")
    if not isinstance(data, dict):
        raise ValueError("Top-level mapping file structure must be an object/dictionary")
    return data


def load_ontology(definition: OntologyDefinition) -> OntologyCatalog:
    try:
        from rdflib import BNode, Graph, Literal, OWL, RDF, RDFS, URIRef
    except ImportError as exc:
        raise RuntimeError("rdflib is required to parse ontology files") from exc

    graph = Graph()
    try:
        graph.parse(definition.file, format=definition.format)
    except Exception as exc:
        raise RuntimeError(f"Failed to parse ontology file '{definition.file}'") from exc

    catalog = OntologyCatalog()

    def label_for(subject: Any) -> Optional[str]:
        literal = graph.value(subject, RDFS.label)
        return str(literal) if isinstance(literal, Literal) else None

    def comment_for(subject: Any) -> Optional[str]:
        literal = graph.value(subject, RDFS.comment)
        return str(literal) if isinstance(literal, Literal) else None

    def restriction_target(node: Any) -> Tuple[Optional[str], Optional[str]]:
        if isinstance(node, URIRef):
            iri = str(node)
            if iri in XSD_TO_JANUSGRAPH:
                return None, infer_data_type_from_ranges({iri})
            catalog.ensure_class(iri)
            return iri, None
        return None, None

    def restriction_literal_value(literal: Literal) -> Tuple[Any, Optional[str]]:
        data_type = infer_data_type_from_ranges({str(literal.datatype)}) if literal.datatype else "String"
        return coerce_value_for_transport(literal.toPython(), data_type), data_type

    def parse_restriction(owner_iri: str, restriction_node: Any) -> None:
        if (restriction_node, RDF.type, OWL.Restriction) not in graph:
            return

        on_property = graph.value(restriction_node, OWL.onProperty)
        if not isinstance(on_property, URIRef):
            return

        property_iri = str(on_property)
        catalog.ensure_property(property_iri)
        filler_iri, filler_data_type = restriction_target(
            graph.value(restriction_node, OWL.onClass) or graph.value(restriction_node, OWL.onDataRange)
        )

        def add_cardinality(predicate: Any, constraint_type: str) -> None:
            literal = graph.value(restriction_node, predicate)
            if isinstance(literal, Literal):
                catalog.add_restriction(
                    OntologyRestriction(
                        source_class_iri=owner_iri,
                        property_iri=property_iri,
                        constraint_type=constraint_type,
                        cardinality=int(literal),
                        filler_iri=filler_iri,
                        filler_data_type=filler_data_type,
                    )
                )

        add_cardinality(OWL.minCardinality, "min_cardinality")
        add_cardinality(OWL.minQualifiedCardinality, "min_cardinality")
        add_cardinality(OWL.maxCardinality, "max_cardinality")
        add_cardinality(OWL.maxQualifiedCardinality, "max_cardinality")
        add_cardinality(OWL.cardinality, "exact_cardinality")
        add_cardinality(OWL.qualifiedCardinality, "exact_cardinality")

        some_values_from = graph.value(restriction_node, OWL.someValuesFrom)
        if some_values_from is not None:
            some_filler_iri, some_filler_data_type = restriction_target(some_values_from)
            catalog.add_restriction(
                OntologyRestriction(
                    source_class_iri=owner_iri,
                    property_iri=property_iri,
                    constraint_type="some_values_from",
                    filler_iri=some_filler_iri,
                    filler_data_type=some_filler_data_type,
                )
            )

        all_values_from = graph.value(restriction_node, OWL.allValuesFrom)
        if all_values_from is not None:
            all_filler_iri, all_filler_data_type = restriction_target(all_values_from)
            catalog.add_restriction(
                OntologyRestriction(
                    source_class_iri=owner_iri,
                    property_iri=property_iri,
                    constraint_type="all_values_from",
                    filler_iri=all_filler_iri,
                    filler_data_type=all_filler_data_type,
                )
            )

        has_value = graph.value(restriction_node, OWL.hasValue)
        if has_value is not None:
            if isinstance(has_value, Literal):
                value, value_data_type = restriction_literal_value(has_value)
                catalog.add_restriction(
                    OntologyRestriction(
                        source_class_iri=owner_iri,
                        property_iri=property_iri,
                        constraint_type="has_value",
                        filler_data_type=value_data_type,
                        has_value=value,
                    )
                )
            elif isinstance(has_value, URIRef):
                catalog.add_restriction(
                    OntologyRestriction(
                        source_class_iri=owner_iri,
                        property_iri=property_iri,
                        constraint_type="has_value",
                        filler_iri=str(has_value),
                        has_value=str(has_value),
                    )
                )

    class_subjects = set(graph.subjects(RDF.type, OWL.Class)) | set(graph.subjects(RDF.type, RDFS.Class))
    for subject in class_subjects:
        if isinstance(subject, BNode):
            continue
        iri = str(subject)
        ontology_class = catalog.ensure_class(iri)
        ontology_class.label = ontology_class.label or label_for(subject)
        ontology_class.comment = ontology_class.comment or comment_for(subject)

    for subject, superclass in graph.subject_objects(RDFS.subClassOf):
        if isinstance(subject, BNode) or not isinstance(subject, URIRef) or not isinstance(superclass, URIRef):
            continue
        catalog.ensure_class(str(subject)).superclasses.add(str(superclass))
        catalog.ensure_class(str(superclass))

    for subject, equivalent in graph.subject_objects(OWL.equivalentClass):
        if isinstance(subject, BNode) or not isinstance(subject, URIRef):
            continue
        if isinstance(equivalent, URIRef):
            catalog.ensure_class(str(subject)).equivalent_classes.add(str(equivalent))
            catalog.ensure_class(str(equivalent)).equivalent_classes.add(str(subject))
            catalog.ensure_class(str(equivalent))
        elif isinstance(equivalent, BNode):
            parse_restriction(str(subject), equivalent)

    for subject, disjoint in graph.subject_objects(OWL.disjointWith):
        if isinstance(subject, BNode) or not isinstance(subject, URIRef) or not isinstance(disjoint, URIRef):
            continue
        catalog.ensure_class(str(subject)).disjoint_classes.add(str(disjoint))
        catalog.ensure_class(str(disjoint)).disjoint_classes.add(str(subject))
        catalog.ensure_class(str(disjoint))

    for subject, superclass in graph.subject_objects(RDFS.subClassOf):
        if isinstance(subject, URIRef) and isinstance(superclass, BNode):
            parse_restriction(str(subject), superclass)

    property_subjects = (
        set(graph.subjects(RDF.type, OWL.ObjectProperty))
        | set(graph.subjects(RDF.type, OWL.DatatypeProperty))
        | set(graph.subjects(RDF.type, RDF.Property))
        | set(graph.subjects(RDFS.subPropertyOf, None))
        | set(graph.subjects(OWL.equivalentProperty, None))
        | set(graph.subjects(OWL.inverseOf, None))
    )
    for subject in property_subjects:
        if isinstance(subject, BNode):
            continue
        iri = str(subject)
        if (subject, RDF.type, OWL.ObjectProperty) in graph:
            kind = "object"
        elif (subject, RDF.type, OWL.DatatypeProperty) in graph:
            kind = "datatype"
        else:
            kind = "unknown"

        ontology_property = catalog.ensure_property(iri, kind=kind)
        ontology_property.label = ontology_property.label or label_for(subject)
        ontology_property.comment = ontology_property.comment or comment_for(subject)

        for domain in graph.objects(subject, RDFS.domain):
            if isinstance(domain, URIRef):
                catalog.ensure_class(str(domain))
                ontology_property.domains.add(str(domain))

        for range_value in graph.objects(subject, RDFS.range):
            if isinstance(range_value, URIRef):
                ontology_property.ranges.add(str(range_value))
                if str(range_value) not in XSD_TO_JANUSGRAPH:
                    catalog.ensure_class(str(range_value))

        if ontology_property.kind == "unknown" and any(range_value in XSD_TO_JANUSGRAPH for range_value in ontology_property.ranges):
            ontology_property.kind = "datatype"

    for subject, superproperty in graph.subject_objects(RDFS.subPropertyOf):
        if isinstance(subject, BNode) or not isinstance(subject, URIRef) or not isinstance(superproperty, URIRef):
            continue
        property_iri = str(subject)
        superproperty_iri = str(superproperty)
        catalog.ensure_property(property_iri).superproperties.add(superproperty_iri)
        catalog.ensure_property(superproperty_iri)

    for subject, equivalent in graph.subject_objects(OWL.equivalentProperty):
        if isinstance(subject, BNode) or not isinstance(subject, URIRef) or not isinstance(equivalent, URIRef):
            continue
        property_iri = str(subject)
        equivalent_iri = str(equivalent)
        catalog.ensure_property(property_iri).equivalent_properties.add(equivalent_iri)
        catalog.ensure_property(equivalent_iri).equivalent_properties.add(property_iri)
        catalog.ensure_property(equivalent_iri)

    for subject, inverse in graph.subject_objects(OWL.inverseOf):
        if isinstance(subject, BNode) or not isinstance(subject, URIRef) or not isinstance(inverse, URIRef):
            continue
        property_iri = str(subject)
        inverse_iri = str(inverse)
        catalog.ensure_property(property_iri, kind="object").inverse_properties.add(inverse_iri)
        catalog.ensure_property(inverse_iri, kind="object").inverse_properties.add(property_iri)

    property_characteristic_types = {
        OWL.FunctionalProperty: "functional",
        OWL.InverseFunctionalProperty: "inverse_functional",
        OWL.TransitiveProperty: "transitive",
        OWL.SymmetricProperty: "symmetric",
        OWL.AsymmetricProperty: "asymmetric",
        OWL.ReflexiveProperty: "reflexive",
        OWL.IrreflexiveProperty: "irreflexive",
    }
    for rdf_type, characteristic_name in property_characteristic_types.items():
        for subject in graph.subjects(RDF.type, rdf_type):
            if isinstance(subject, BNode):
                continue
            property_iri = str(subject)
            kind = "object" if rdf_type != OWL.FunctionalProperty else "unknown"
            catalog.ensure_property(property_iri, kind=kind).characteristics.add(characteristic_name)

    for ontology_property in catalog.properties.values():
        if ontology_property.kind == "unknown":
            if ontology_property.inverse_properties or any(
                characteristic in ontology_property.characteristics
                for characteristic in {"inverse_functional", "transitive", "symmetric", "asymmetric", "reflexive", "irreflexive"}
            ):
                ontology_property.kind = "object"
            elif any(range_value in XSD_TO_JANUSGRAPH for range_value in ontology_property.ranges):
                ontology_property.kind = "datatype"
            elif ontology_property.ranges:
                ontology_property.kind = "object"

    LOG.info(
        "Parsed ontology '%s' with %s classes and %s properties",
        definition.file,
        len(catalog.classes),
        len(catalog.properties),
    )
    return catalog


def validate_mapping_against_ontology(config: LoaderConfig, ontology: OntologyCatalog) -> None:
    issues: List[str] = []
    classes_by_iri = {item.iri: item for item in config.classes}
    reported_disjoint_pairs: set[Tuple[str, str]] = set()

    for class_mapping in config.classes:
        if class_mapping.iri not in ontology.classes:
            issues.append(f"Class IRI '{class_mapping.iri}' is not present in the ontology")

        lineage = ontology.class_lineage(class_mapping.iri)
        for lineage_class_iri in lineage:
            ontology_class = ontology.classes.get(lineage_class_iri)
            if not ontology_class:
                continue
            for disjoint_iri in ontology_class.disjoint_classes:
                if disjoint_iri in lineage:
                    pair = tuple(sorted((lineage_class_iri, disjoint_iri)))
                    if pair in reported_disjoint_pairs:
                        continue
                    reported_disjoint_pairs.add(pair)
                    issues.append(
                        f"Class '{class_mapping.iri}' has an inconsistent ontology lineage containing disjoint classes "
                        f"'{pair[0]}' and '{pair[1]}'"
                    )

        for prop in class_mapping.properties:
            ontology_property = ontology.properties.get(prop.iri)
            if ontology_property is None:
                issues.append(f"Property IRI '{prop.iri}' is not present in the ontology")
            elif ontology_property.kind == "object":
                issues.append(f"Property IRI '{prop.iri}' is an object property but is mapped as a vertex attribute")
            else:
                effective_domains = {iri for iri in ontology.effective_property_domains(prop.iri) if iri in ontology.classes}
                if effective_domains and not any(
                    ontology.class_is_assignable_to(class_mapping.iri, domain_iri) for domain_iri in effective_domains
                ):
                    issues.append(
                        f"Property IRI '{prop.iri}' expects domain(s) {sorted(effective_domains)} but is mapped to class "
                        f"'{class_mapping.iri}' which is not compatible through subclass/equivalent-class reasoning"
                    )

                max_cardinality = ontology.max_cardinality_for_class_property(class_mapping.iri, prop.iri)
                characteristics = ontology.effective_property_characteristics(prop.iri)
                if (max_cardinality == 1 or "functional" in characteristics) and normalize_cardinality(prop.cardinality) != "SINGLE":
                    issues.append(
                        f"Property IRI '{prop.iri}' is constrained to at most one value by the ontology but mapping cardinality "
                        f"is '{prop.cardinality}'"
                    )

                property_lineage = ontology.property_lineage(prop.iri)
                actual_data_type = normalize_data_type(
                    prop.data_type or infer_data_type_from_ranges(ontology.effective_property_ranges(prop.iri))
                )
                for restriction in ontology.restrictions_for_class(class_mapping.iri):
                    if restriction.property_iri not in property_lineage:
                        continue
                    if restriction.filler_data_type and actual_data_type != normalize_data_type(restriction.filler_data_type):
                        issues.append(
                            f"Property IRI '{prop.iri}' is constrained to values of type '{restriction.filler_data_type}' by the ontology, "
                            f"but mapping resolves to data_type '{actual_data_type}'"
                        )

    for relationship in config.relationships:
        ontology_property = ontology.properties.get(relationship.iri)
        if ontology_property is None:
            issues.append(f"Relationship IRI '{relationship.iri}' is not present in the ontology")
        elif ontology_property.kind == "datatype":
            issues.append(f"Relationship IRI '{relationship.iri}' is a datatype property but is mapped as an edge")
        else:
            source_class = classes_by_iri[relationship.source_class_iri]
            target_class = classes_by_iri[relationship.target_class_iri]

            effective_domains = {iri for iri in ontology.effective_property_domains(relationship.iri) if iri in ontology.classes}
            if effective_domains and not any(
                ontology.class_is_assignable_to(source_class.iri, domain_iri) for domain_iri in effective_domains
            ):
                issues.append(
                    f"Relationship IRI '{relationship.iri}' expects source domain(s) {sorted(effective_domains)} but is mapped from "
                    f"class '{source_class.iri}' which is not compatible through subclass/equivalent-class reasoning"
                )

            effective_ranges = {iri for iri in ontology.effective_property_ranges(relationship.iri) if iri in ontology.classes}
            if effective_ranges and not any(
                ontology.class_is_assignable_to(target_class.iri, range_iri) for range_iri in effective_ranges
            ):
                issues.append(
                    f"Relationship IRI '{relationship.iri}' expects target range(s) {sorted(effective_ranges)} but is mapped to "
                    f"class '{target_class.iri}' which is not compatible through subclass/equivalent-class reasoning"
                )

            effective_characteristics = ontology.effective_property_characteristics(relationship.iri)
            source_max_cardinality = ontology.max_cardinality_for_class_property(source_class.iri, relationship.iri)
            if (source_max_cardinality == 1 or "functional" in effective_characteristics) and not multiplicity_supports_outgoing_functional(
                relationship.multiplicity
            ):
                issues.append(
                    f"Relationship IRI '{relationship.iri}' is constrained to one outgoing target per source in the ontology but "
                    f"mapping multiplicity '{relationship.multiplicity}' does not enforce that. Use MANY2ONE or ONE2ONE."
                )

            if "inverse_functional" in effective_characteristics and not multiplicity_supports_incoming_functional(
                relationship.multiplicity
            ):
                issues.append(
                    f"Relationship IRI '{relationship.iri}' is inverse-functional in the ontology but mapping multiplicity "
                    f"'{relationship.multiplicity}' does not enforce one incoming source. Use ONE2MANY or ONE2ONE."
                )

            relationship_lineage = ontology.property_lineage(relationship.iri)
            for restriction in ontology.restrictions_for_class(source_class.iri):
                if restriction.property_iri not in relationship_lineage:
                    continue
                if restriction.constraint_type == "has_value":
                    continue
                if restriction.filler_iri and not ontology.class_is_assignable_to(target_class.iri, restriction.filler_iri):
                    issues.append(
                        f"Relationship IRI '{relationship.iri}' is constrained to target class '{restriction.filler_iri}' by the ontology, "
                        f"but mapping targets class '{target_class.iri}'"
                    )

    if issues:
        message = "Ontology validation issues detected:\n- " + "\n- ".join(issues)
        if config.runtime.strict_ontology:
            raise ValueError(message)
        LOG.warning(message)


class SchemaPlanner:
    def __init__(self, config: LoaderConfig, ontology: OntologyCatalog) -> None:
        self.config = config
        self.ontology = ontology
        self._name_registry: Dict[Tuple[str, str], str] = {}

    def build(self) -> SchemaPlan:
        plan = SchemaPlan()

        for spec in SYSTEM_VERTEX_PROPERTY_SPECS:
            self._register_name("property", spec["name"], f"system:{spec['name']}")
            plan.ensure_property_key(spec["name"], spec["data_type"], spec["cardinality"])
        for spec in SYSTEM_EDGE_PROPERTY_SPECS:
            self._register_name("property", spec["name"], f"system:{spec['name']}")
            plan.ensure_property_key(spec["name"], spec["data_type"], spec["cardinality"])

        mapped_class_iris = set()
        mapped_property_iris = set()
        mapped_relationship_iris = set()

        for class_mapping in self.config.classes:
            mapped_class_iris.add(class_mapping.iri)
            self._register_name("vertex_label", class_mapping.resolved_vertex_label(), class_mapping.iri)
            plan.ensure_vertex_label(class_mapping.resolved_vertex_label(), class_mapping.iri)
            for prop in class_mapping.properties:
                mapped_property_iris.add(prop.iri)
                self._register_name("property", prop.resolved_property_key(), prop.iri)
                plan.ensure_property_key(
                    prop.resolved_property_key(),
                    self.resolve_property_data_type(prop),
                    prop.cardinality,
                )

        for relationship in self.config.relationships:
            mapped_relationship_iris.add(relationship.iri)
            self._register_name("edge_label", relationship.resolved_edge_label(), relationship.iri)
            plan.ensure_edge_label(
                relationship.resolved_edge_label(),
                relationship.multiplicity,
                relationship.iri,
            )
            for prop in relationship.properties:
                mapped_property_iris.add(prop.iri)
                self._register_name("property", prop.resolved_property_key(), prop.iri)
                plan.ensure_property_key(
                    prop.resolved_property_key(),
                    self.resolve_property_data_type(prop),
                    prop.cardinality,
                )

        if self.config.ontology.include_unmapped_terms:
            for ontology_class in self.ontology.classes.values():
                if ontology_class.iri not in mapped_class_iris:
                    label = safe_name(local_name(ontology_class.iri))
                    self._register_name("vertex_label", label, ontology_class.iri)
                    plan.ensure_vertex_label(label, ontology_class.iri)

            for ontology_property in self.ontology.properties.values():
                if ontology_property.kind == "object":
                    if ontology_property.iri not in mapped_relationship_iris:
                        label = safe_name(local_name(ontology_property.iri))
                        self._register_name("edge_label", label, ontology_property.iri)
                        plan.ensure_edge_label(
                            label,
                            "MULTI",
                            ontology_property.iri,
                        )
                else:
                    if ontology_property.iri not in mapped_property_iris:
                        key = safe_name(local_name(ontology_property.iri))
                        self._register_name("property", key, ontology_property.iri)
                        plan.ensure_property_key(
                            key,
                            infer_data_type_from_ranges(ontology_property.ranges),
                            "SINGLE",
                        )

        plan.ensure_vertex_index("byExternalId", "external_id", True)
        plan.ensure_edge_index("byEdgeExternalId", "edge_external_id", False)
        return plan

    def resolve_property_data_type(self, prop: PropertyMapping) -> str:
        if prop.data_type:
            return normalize_data_type(prop.data_type)
        ontology_property = self.ontology.properties.get(prop.iri)
        if ontology_property:
            return infer_data_type_from_ranges(ontology_property.ranges)
        return "String"

    def _register_name(self, namespace: str, name: str, iri: str) -> None:
        key = (namespace, name)
        existing = self._name_registry.get(key)
        if existing and existing != iri:
            raise ValueError(
                f"Name collision for {namespace} '{name}': both '{existing}' and '{iri}' resolve to the same JanusGraph name"
            )
        self._name_registry[key] = iri


def build_select_sql(table: str, columns: Sequence[str], where_clause: Optional[str] = None) -> str:
    validate_sql_fragment(table, "table reference")
    select_columns = unique_preserve_order(list(columns))
    for column in select_columns:
        validate_sql_fragment(column, "column projection")
    sql = f"SELECT {', '.join(select_columns)} FROM {table}"
    if where_clause:
        validate_sql_fragment(where_clause, "where clause")
        sql += f" WHERE {where_clause}"
    return sql


def deduplicate_payload_rows(rows: List[Dict[str, Any]], identity_key: str) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for row in rows:
        row_id = row[identity_key]
        if row_id not in merged:
            copied = dict(row)
            copied["properties"] = list(row.get("properties", []))
            merged[row_id] = copied
            order.append(row_id)
            continue
        merged[row_id]["properties"].extend(row.get("properties", []))
    return [merged[item] for item in order]


def split_class_properties(class_mapping: ClassMapping) -> Tuple[List[PropertyMapping], List[PropertyMapping]]:
    inline: List[PropertyMapping] = []
    external: List[PropertyMapping] = []

    for prop in class_mapping.properties:
        if class_mapping.source.sql:
            inlineable = not prop.source_table and not prop.source_sql and not prop.where and not prop.entity_key_column
        else:
            source_table = prop.source_table or class_mapping.source.table
            entity_key_column = prop.entity_key_column or class_mapping.source.key_column
            inlineable = (
                not prop.source_sql
                and not prop.where
                and source_table == class_mapping.source.table
                and entity_key_column == class_mapping.source.key_column
            )
        if inlineable:
            inline.append(prop)
        else:
            external.append(prop)
    return inline, external


def build_class_sql(class_mapping: ClassMapping, inline_properties: Sequence[PropertyMapping]) -> str:
    if class_mapping.source.sql:
        return class_mapping.source.sql

    if not class_mapping.source.table or not class_mapping.source.key_column:
        raise ValueError(f"Class '{class_mapping.iri}' needs source.table and source.key_column for generated SQL")

    columns = [class_mapping.source.key_column]
    columns.extend(prop.source_column for prop in inline_properties if prop.source_column)
    return build_select_sql(class_mapping.source.table, columns, class_mapping.source.where)


def build_property_sql(class_mapping: ClassMapping, prop: PropertyMapping) -> str:
    if prop.source_sql:
        return prop.source_sql

    source_table = prop.source_table or class_mapping.source.table
    entity_key_column = prop.entity_key_column or class_mapping.source.key_column
    class_key = class_mapping.source.key_column
    if not source_table or not entity_key_column or not class_key or not prop.source_column:
        raise ValueError(f"Property '{prop.iri}' does not have enough metadata to build SQL")

    entity_projection = entity_key_column
    if entity_key_column != class_key:
        entity_projection = f"{entity_key_column} AS {class_key}"
    columns = [entity_projection, prop.source_column]
    return build_select_sql(source_table, columns, prop.where)


def build_relationship_sql(
    relationship: RelationshipMapping,
    source_class: ClassMapping,
    target_class: ClassMapping,
) -> str:
    if relationship.source.sql:
        return relationship.source.sql

    if not relationship.source.table:
        raise ValueError(f"Relationship '{relationship.iri}' needs source.table or source.sql")
    if not relationship.source.from_column or not relationship.source.to_column:
        raise ValueError(f"Relationship '{relationship.iri}' needs source.from_column and source.to_column")
    if not source_class.source.key_column or not target_class.source.key_column:
        raise ValueError(f"Relationship '{relationship.iri}' requires source/target class key columns")

    columns: List[str] = []
    if relationship.source.from_column == source_class.source.key_column:
        columns.append(relationship.source.from_column)
    else:
        columns.append(f"{relationship.source.from_column} AS {source_class.source.key_column}")

    if relationship.source.to_column == target_class.source.key_column:
        columns.append(relationship.source.to_column)
    else:
        columns.append(f"{relationship.source.to_column} AS {target_class.source.key_column}")

    columns.extend(prop.source_column for prop in relationship.properties if prop.source_column)
    return build_select_sql(relationship.source.table, columns, relationship.source.where)


def _clean_csv_value(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped != "" else None


class TabularDataClient:
    def describe_class_source(self, class_mapping: ClassMapping, inline_properties: Sequence[PropertyMapping]) -> Dict[str, Any]:
        raise NotImplementedError

    def iter_class_batches(
        self,
        class_mapping: ClassMapping,
        inline_properties: Sequence[PropertyMapping],
        fetch_size: int,
    ) -> Iterator[List[Dict[str, Any]]]:
        raise NotImplementedError

    def describe_property_source(self, class_mapping: ClassMapping, prop: PropertyMapping) -> Dict[str, Any]:
        raise NotImplementedError

    def iter_property_batches(
        self,
        class_mapping: ClassMapping,
        prop: PropertyMapping,
        fetch_size: int,
    ) -> Iterator[List[Dict[str, Any]]]:
        raise NotImplementedError

    def describe_relationship_source(
        self,
        relationship: RelationshipMapping,
        source_class: ClassMapping,
        target_class: ClassMapping,
    ) -> Dict[str, Any]:
        raise NotImplementedError

    def iter_relationship_batches(
        self,
        relationship: RelationshipMapping,
        source_class: ClassMapping,
        target_class: ClassMapping,
        fetch_size: int,
    ) -> Iterator[List[Dict[str, Any]]]:
        raise NotImplementedError

    def close(self) -> None:
        return None


class TeradataClient(TabularDataClient):
    def __init__(self) -> None:
        self._connection: Any = None

    def connect(self) -> Any:
        if self._connection is not None:
            return self._connection

        try:
            import teradatasql
        except ImportError as exc:
            raise RuntimeError("teradatasql is required to read data from Teradata") from exc

        host = os.getenv("TERADATA_HOST", "")
        if not host:
            raise RuntimeError("TERADATA_HOST must be set")

        connect_args: Dict[str, Any] = {"host": host}
        optional_env_map = {
            "TERADATA_DATABASE": "database",
            "TERADATA_USER": "user",
            "TERADATA_PASSWORD": "password",
            "TERADATA_LOGMECH": "logmech",
            "TERADATA_ENCRYPTDATA": "encryptdata",
            "TERADATA_TMODE": "tmode",
            "TERADATA_LOGDATA": "logdata",
        }
        for env_name, connect_name in optional_env_map.items():
            value = os.getenv(env_name)
            if value:
                connect_args[connect_name] = value

        extra_params = os.getenv("TERADATA_EXTRA_PARAMS", "{}").strip() or "{}"
        extra = json.loads(extra_params)
        if not isinstance(extra, dict):
            raise ValueError("TERADATA_EXTRA_PARAMS must be a JSON object")
        connect_args.update(extra)

        LOG.info("Connecting to Teradata host '%s' with logmech '%s'", host, connect_args.get("logmech", "default"))
        self._connection = teradatasql.connect(**connect_args)
        return self._connection

    def iter_batches(self, sql: str, fetch_size: int) -> Iterator[List[Dict[str, Any]]]:
        cursor = self.connect().cursor()
        try:
            LOG.info("Executing Teradata query: %s", sql)
            cursor.execute(sql)
            columns = [description[0] for description in cursor.description]
            while True:
                rows = cursor.fetchmany(fetch_size)
                if not rows:
                    break
                yield [normalize_row(columns, row) for row in rows]
        finally:
            cursor.close()

    def describe_class_source(self, class_mapping: ClassMapping, inline_properties: Sequence[PropertyMapping]) -> Dict[str, Any]:
        return {"type": "sql", "sql": build_class_sql(class_mapping, inline_properties)}

    def iter_class_batches(
        self,
        class_mapping: ClassMapping,
        inline_properties: Sequence[PropertyMapping],
        fetch_size: int,
    ) -> Iterator[List[Dict[str, Any]]]:
        sql = build_class_sql(class_mapping, inline_properties)
        yield from self.iter_batches(sql, fetch_size)

    def describe_property_source(self, class_mapping: ClassMapping, prop: PropertyMapping) -> Dict[str, Any]:
        return {"type": "sql", "sql": build_property_sql(class_mapping, prop)}

    def iter_property_batches(
        self,
        class_mapping: ClassMapping,
        prop: PropertyMapping,
        fetch_size: int,
    ) -> Iterator[List[Dict[str, Any]]]:
        sql = build_property_sql(class_mapping, prop)
        yield from self.iter_batches(sql, fetch_size)

    def describe_relationship_source(
        self,
        relationship: RelationshipMapping,
        source_class: ClassMapping,
        target_class: ClassMapping,
    ) -> Dict[str, Any]:
        return {"type": "sql", "sql": build_relationship_sql(relationship, source_class, target_class)}

    def iter_relationship_batches(
        self,
        relationship: RelationshipMapping,
        source_class: ClassMapping,
        target_class: ClassMapping,
        fetch_size: int,
    ) -> Iterator[List[Dict[str, Any]]]:
        sql = build_relationship_sql(relationship, source_class, target_class)
        yield from self.iter_batches(sql, fetch_size)

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None


class CsvClient(TabularDataClient):
    def __init__(self, root_dir: str, delimiter: str = ",", encoding: str = "utf-8") -> None:
        self.root_dir = Path(root_dir)
        self.delimiter = delimiter
        self.encoding = encoding

    def _resolve_table_path(self, table: str) -> Path:
        direct = self.root_dir / table
        if direct.exists():
            return direct

        if not table.endswith(".csv"):
            direct_csv = self.root_dir / f"{table}.csv"
            if direct_csv.exists():
                return direct_csv

        if "." in table:
            leaf = table.rsplit(".", 1)[1]
            leaf_csv = self.root_dir / f"{leaf}.csv"
            if leaf_csv.exists():
                return leaf_csv

        raise FileNotFoundError(f"CSV file for table '{table}' not found under {self.root_dir}")

    def _iter_projected_batches(
        self,
        table: str,
        projections: Sequence[Tuple[str, str]],
        fetch_size: int,
    ) -> Iterator[List[Dict[str, Any]]]:
        path = self._resolve_table_path(table)
        batch: List[Dict[str, Any]] = []
        with path.open("r", encoding=self.encoding, newline="") as handle:
            reader = csv.DictReader(handle, delimiter=self.delimiter)
            if reader.fieldnames is None:
                raise ValueError(f"CSV file '{path}' does not contain a header row")
            available = set(reader.fieldnames)
            required = {source_name for source_name, _ in projections}
            missing = sorted(required - available)
            if missing:
                raise KeyError(
                    f"CSV file '{path}' is missing required column(s) {missing}. Available columns: {sorted(available)}"
                )

            for raw_row in reader:
                projected = {
                    output_name: _clean_csv_value(raw_row.get(source_name))
                    for source_name, output_name in projections
                }
                batch.append(normalize_row(list(projected.keys()), list(projected.values())))
                if len(batch) >= fetch_size:
                    yield batch
                    batch = []

        if batch:
            yield batch

    def describe_class_source(self, class_mapping: ClassMapping, inline_properties: Sequence[PropertyMapping]) -> Dict[str, Any]:
        projections = [class_mapping.source.key_column]
        projections.extend(prop.source_column for prop in inline_properties if prop.source_column)
        return {"type": "csv", "table": class_mapping.source.table, "columns": unique_preserve_order(projections)}

    def iter_class_batches(
        self,
        class_mapping: ClassMapping,
        inline_properties: Sequence[PropertyMapping],
        fetch_size: int,
    ) -> Iterator[List[Dict[str, Any]]]:
        if not class_mapping.source.table or not class_mapping.source.key_column:
            raise ValueError(f"Class '{class_mapping.iri}' requires source.table and source.key_column for CSV mode")
        projections = [(class_mapping.source.key_column, class_mapping.source.key_column)]
        projections.extend((prop.source_column, prop.source_column) for prop in inline_properties if prop.source_column)
        yield from self._iter_projected_batches(class_mapping.source.table, projections, fetch_size)

    def describe_property_source(self, class_mapping: ClassMapping, prop: PropertyMapping) -> Dict[str, Any]:
        return {
            "type": "csv",
            "table": prop.source_table or class_mapping.source.table,
            "columns": [prop.entity_key_column or class_mapping.source.key_column, prop.source_column],
        }

    def iter_property_batches(
        self,
        class_mapping: ClassMapping,
        prop: PropertyMapping,
        fetch_size: int,
    ) -> Iterator[List[Dict[str, Any]]]:
        source_table = prop.source_table or class_mapping.source.table
        entity_key_column = prop.entity_key_column or class_mapping.source.key_column
        class_key = class_mapping.source.key_column
        if not source_table or not entity_key_column or not class_key or not prop.source_column:
            raise ValueError(f"Property '{prop.iri}' does not have enough metadata for CSV mode")

        projections = [
            (entity_key_column, class_key),
            (prop.source_column, prop.source_column),
        ]
        yield from self._iter_projected_batches(source_table, projections, fetch_size)

    def describe_relationship_source(
        self,
        relationship: RelationshipMapping,
        source_class: ClassMapping,
        target_class: ClassMapping,
    ) -> Dict[str, Any]:
        columns = [source_class.source.key_column, target_class.source.key_column]
        columns.extend(prop.source_column for prop in relationship.properties if prop.source_column)
        return {"type": "csv", "table": relationship.source.table, "columns": unique_preserve_order(columns)}

    def iter_relationship_batches(
        self,
        relationship: RelationshipMapping,
        source_class: ClassMapping,
        target_class: ClassMapping,
        fetch_size: int,
    ) -> Iterator[List[Dict[str, Any]]]:
        if not relationship.source.table or not relationship.source.from_column or not relationship.source.to_column:
            raise ValueError(f"Relationship '{relationship.iri}' requires source table/from/to columns for CSV mode")
        if not source_class.source.key_column or not target_class.source.key_column:
            raise ValueError(f"Relationship '{relationship.iri}' requires source/target class key columns")

        projections = [
            (relationship.source.from_column, source_class.source.key_column),
            (relationship.source.to_column, target_class.source.key_column),
        ]
        projections.extend((prop.source_column, prop.source_column) for prop in relationship.properties if prop.source_column)
        yield from self._iter_projected_batches(relationship.source.table, projections, fetch_size)


class JanusGraphClient:
    def __init__(self, settings: JanusGraphSettings) -> None:
        self.settings = settings
        self._client: Any = None

    def connect(self) -> None:
        if self._client is not None:
            return

        if not self.settings.url:
            raise RuntimeError("JanusGraph URL must be set in mapping file or JANUSGRAPH_URL")

        try:
            from gremlin_python.driver.aiohttp.transport import AiohttpTransport
            from gremlin_python.driver.client import Client
            from gremlin_python.driver.serializer import GraphSONSerializersV3d0
        except ImportError as exc:
            raise RuntimeError("gremlinpython is required to write data to JanusGraph") from exc

        transport_factory = None
        ssl_context = self._build_ssl_context()
        if ssl_context is not None:
            timeout = self.settings.request_timeout_seconds
            transport_factory = lambda: AiohttpTransport(
                read_timeout=timeout,
                write_timeout=timeout,
                ssl_context=ssl_context,
            )

        self._client = Client(
            self.settings.url,
            self.settings.traversal_source,
            username=os.getenv("JANUSGRAPH_USERNAME") or None,
            password=os.getenv("JANUSGRAPH_PASSWORD") or None,
            message_serializer=GraphSONSerializersV3d0(),
            transport_factory=transport_factory,
        )

    def _build_ssl_context(self) -> Optional[ssl.SSLContext]:
        if not self.settings.url.lower().startswith("wss://"):
            return None

        verify = as_bool(os.getenv("JANUSGRAPH_SSL_VERIFY", "true"), True)
        ssl_context = ssl.create_default_context()
        ca_file = os.getenv("JANUSGRAPH_SSL_CA_FILE")
        cert_file = os.getenv("JANUSGRAPH_SSL_CERT_FILE")
        key_file = os.getenv("JANUSGRAPH_SSL_KEY_FILE")

        if ca_file:
            ssl_context.load_verify_locations(cafile=ca_file)
        if cert_file:
            ssl_context.load_cert_chain(certfile=cert_file, keyfile=key_file or None)
        if not verify:
            LOG.warning("JANUSGRAPH_SSL_VERIFY is false; TLS certificate verification is disabled")
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        return ssl_context

    def submit(self, script: str, bindings: Optional[Dict[str, Any]] = None, include_graph: bool = False) -> List[Any]:
        self.connect()
        request_options = None
        if include_graph:
            request_options = {
                "aliases": {
                    self.settings.traversal_source: self.settings.traversal_source,
                    self.settings.graph_alias: self.settings.graph_alias,
                }
            }

        try:
            result_set = self._client.submit(script, bindings=bindings or {}, request_options=request_options)
            return result_set.all().result()
        except Exception:
            LOG.exception(
                "Gremlin submission failed (script_bytes=%s, binding_keys=%s)",
                len(script.encode("utf-8")),
                sorted((bindings or {}).keys()),
            )
            raise

    def ensure_schema(self, schema_plan: SchemaPlan) -> None:
        LOG.info(
            "Ensuring JanusGraph schema: %s vertex labels, %s edge labels, %s property keys",
            len(schema_plan.vertex_labels),
            len(schema_plan.edge_labels),
            len(schema_plan.property_keys),
        )
        self.submit(
            build_management_script(self.settings.graph_alias),
            bindings={"schema": schema_plan.as_binding()},
            include_graph=True,
        )

    def upsert_vertex_batch(self, label: str, rows: List[Dict[str, Any]]) -> int:
        if not rows:
            return 0
        rows = deduplicate_payload_rows(rows, "external_id")
        result = self.submit(
            VERTEX_BATCH_SCRIPT,
            bindings={"vertexLabel": label, "idKey": "external_id", "rows": rows},
        )
        return int(result[0]) if result else 0

    def update_vertex_properties_batch(self, label: str, rows: List[Dict[str, Any]]) -> Dict[str, int]:
        if not rows:
            return {"processed": 0, "missing": 0}
        rows = deduplicate_payload_rows(rows, "external_id")
        result = self.submit(
            VERTEX_PROPERTY_UPDATE_SCRIPT,
            bindings={"vertexLabel": label, "idKey": "external_id", "rows": rows},
        )
        counters = dict(result[0]) if result else {}
        return {"processed": int(counters.get("processed", 0)), "missing": int(counters.get("missing", 0))}

    def upsert_edge_batch(
        self,
        edge_label: str,
        out_label: str,
        in_label: str,
        rows: List[Dict[str, Any]],
    ) -> Dict[str, int]:
        if not rows:
            return {"processed": 0, "missing": 0}
        rows = deduplicate_payload_rows(rows, "edge_external_id")
        result = self.submit(
            EDGE_BATCH_SCRIPT,
            bindings={
                "edgeLabel": edge_label,
                "outLabel": out_label,
                "inLabel": in_label,
                "idKey": "external_id",
                "edgeIdKey": "edge_external_id",
                "rows": rows,
            },
        )
        counters = dict(result[0]) if result else {}
        return {"processed": int(counters.get("processed", 0)), "missing": int(counters.get("missing", 0))}

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


class KnowledgeGraphLoader:
    def __init__(self, config: LoaderConfig) -> None:
        self.config = config
        self.classes_by_iri = {item.iri: item for item in config.classes}

    def build_data_client(self) -> TabularDataClient:
        if self.config.ingestion.source_type == "csv":
            return CsvClient(
                root_dir=self.config.ingestion.csv_root_dir or "",
                delimiter=self.config.ingestion.csv_delimiter,
                encoding=self.config.ingestion.csv_encoding,
            )
        return TeradataClient()

    def run(self) -> Dict[str, Any]:
        started_at = datetime.now(timezone.utc)
        batch_id = started_at.strftime("%Y%m%dT%H%M%SZ")
        loaded_at = to_iso_instant(started_at)

        ontology = load_ontology(self.config.ontology)
        validate_mapping_against_ontology(self.config, ontology)
        schema_plan = SchemaPlanner(self.config, ontology).build()

        report: Dict[str, Any] = {
            "batch_id": batch_id,
            "started_at": loaded_at,
            "dry_run": self.config.runtime.dry_run,
            "mode": self.config.ingestion.mode,
            "source_type": self.config.ingestion.source_type,
            "schema": schema_plan.to_dict(),
            "classes": {},
            "relationships": {},
        }

        data_client = self.build_data_client()

        if self.config.runtime.dry_run:
            report["generated_sources"] = self.preview_sources(data_client)
            report["finished_at"] = to_iso_instant(datetime.now(timezone.utc))
            return report

        janusgraph = JanusGraphClient(self.config.janusgraph)
        try:
            if self.config.janusgraph.create_schema:
                janusgraph.ensure_schema(schema_plan)

            for class_mapping in self.config.classes:
                report["classes"][class_mapping.resolved_vertex_label()] = self.load_class(
                    data_client=data_client,
                    janusgraph=janusgraph,
                    ontology=ontology,
                    class_mapping=class_mapping,
                    batch_id=batch_id,
                    loaded_at=loaded_at,
                )

            for relationship in self.config.relationships:
                report["relationships"][relationship.resolved_edge_label()] = self.load_relationship(
                    data_client=data_client,
                    janusgraph=janusgraph,
                    ontology=ontology,
                    relationship=relationship,
                    batch_id=batch_id,
                    loaded_at=loaded_at,
                )
        finally:
            data_client.close()
            janusgraph.close()

        report["finished_at"] = to_iso_instant(datetime.now(timezone.utc))
        return report

    def preview_sources(self, data_client: TabularDataClient) -> Dict[str, Any]:
        source_preview: Dict[str, Any] = {"classes": {}, "relationships": {}}
        for class_mapping in self.config.classes:
            inline, external = split_class_properties(class_mapping)
            source_preview["classes"][class_mapping.resolved_vertex_label()] = {
                "base": data_client.describe_class_source(class_mapping, inline),
                "external_properties": {
                    prop.resolved_property_key(): data_client.describe_property_source(class_mapping, prop)
                    for prop in external
                },
            }
        for relationship in self.config.relationships:
            source_class = self.classes_by_iri[relationship.source_class_iri]
            target_class = self.classes_by_iri[relationship.target_class_iri]
            source_preview["relationships"][relationship.resolved_edge_label()] = data_client.describe_relationship_source(
                relationship,
                source_class,
                target_class,
            )
        return source_preview

    def load_class(
        self,
        data_client: TabularDataClient,
        janusgraph: JanusGraphClient,
        ontology: OntologyCatalog,
        class_mapping: ClassMapping,
        batch_id: str,
        loaded_at: str,
    ) -> Dict[str, Any]:
        inline_props, external_props = split_class_properties(class_mapping)
        label = class_mapping.resolved_vertex_label()

        stats: Dict[str, Any] = {
            "label": label,
            "base_source": data_client.describe_class_source(class_mapping, inline_props),
            "vertices_loaded": 0,
            "attribute_updates": 0,
            "missing_vertices": 0,
        }

        base_fetch_size = class_mapping.source.fetch_size or self.config.runtime.batch_size
        for raw_batch in data_client.iter_class_batches(class_mapping, inline_props, base_fetch_size):
            for batch in chunked(raw_batch, self.config.runtime.batch_size):
                payload: List[Dict[str, Any]] = []
                for row in batch:
                    payload.append(
                        {
                            "external_id": render_template(class_mapping.resolved_id_template(), row),
                            "properties": self.build_vertex_properties(
                                ontology=ontology,
                                class_mapping=class_mapping,
                                row=row,
                                props=inline_props,
                                batch_id=batch_id,
                                loaded_at=loaded_at,
                            ),
                        }
                    )
                stats["vertices_loaded"] += janusgraph.upsert_vertex_batch(label, payload)

        external_queries: Dict[str, Any] = {}
        for prop in external_props:
            external_queries[prop.resolved_property_key()] = data_client.describe_property_source(class_mapping, prop)
            fetch_size = class_mapping.source.fetch_size or self.config.runtime.batch_size
            for raw_batch in data_client.iter_property_batches(class_mapping, prop, fetch_size):
                for batch in chunked(raw_batch, self.config.runtime.batch_size):
                    payload = []
                    for row in batch:
                        property_spec = self.build_single_property(ontology, prop, row)
                        if property_spec is None:
                            continue
                        payload.append(
                            {
                                "external_id": render_template(class_mapping.resolved_id_template(), row),
                                "properties": [
                                    property_spec,
                                    {
                                        "key": "load_batch_id",
                                        "value": batch_id,
                                        "data_type": "String",
                                        "cardinality": "SINGLE",
                                    },
                                    {
                                        "key": "loaded_at",
                                        "value": loaded_at,
                                        "data_type": "Instant",
                                        "cardinality": "SINGLE",
                                    },
                                ],
                            }
                        )
                    result = janusgraph.update_vertex_properties_batch(label, payload)
                    stats["attribute_updates"] += result["processed"]
                    stats["missing_vertices"] += result["missing"]

        stats["external_property_queries"] = external_queries
        return stats

    def load_relationship(
        self,
        data_client: TabularDataClient,
        janusgraph: JanusGraphClient,
        ontology: OntologyCatalog,
        relationship: RelationshipMapping,
        batch_id: str,
        loaded_at: str,
    ) -> Dict[str, Any]:
        source_class = self.classes_by_iri[relationship.source_class_iri]
        target_class = self.classes_by_iri[relationship.target_class_iri]
        source_descriptor = data_client.describe_relationship_source(relationship, source_class, target_class)

        stats: Dict[str, Any] = {
            "label": relationship.resolved_edge_label(),
            "source": source_descriptor,
            "edges_loaded": 0,
            "missing_vertices": 0,
        }

        fetch_size = relationship.source.fetch_size or self.config.runtime.batch_size
        for raw_batch in data_client.iter_relationship_batches(relationship, source_class, target_class, fetch_size):
            for batch in chunked(raw_batch, self.config.runtime.batch_size):
                payload = []
                for row in batch:
                    property_lineage = encode_sorted_string_set(ontology.property_lineage(relationship.iri))
                    inverse_properties = encode_sorted_string_set(ontology.inverse_properties_for(relationship.iri))
                    edge_properties = [
                        {
                            "key": "ontology_iri",
                            "value": relationship.iri,
                            "data_type": "String",
                            "cardinality": "SINGLE",
                        },
                        {
                            "key": "ontology_property_lineage",
                            "value": property_lineage,
                            "data_type": "String",
                            "cardinality": "SINGLE",
                        },
                        {
                            "key": "ontology_inverse_property_iris",
                            "value": inverse_properties,
                            "data_type": "String",
                            "cardinality": "SINGLE",
                        },
                        {
                            "key": "source_table",
                            "value": relationship.source.table or "custom_sql",
                            "data_type": "String",
                            "cardinality": "SINGLE",
                        },
                        {
                            "key": "load_batch_id",
                            "value": batch_id,
                            "data_type": "String",
                            "cardinality": "SINGLE",
                        },
                        {
                            "key": "loaded_at",
                            "value": loaded_at,
                            "data_type": "Instant",
                            "cardinality": "SINGLE",
                        },
                    ]
                    for prop in relationship.properties:
                        property_spec = self.build_single_property(ontology, prop, row)
                        if property_spec is not None:
                            edge_properties.append(property_spec)

                    payload.append(
                        {
                            "out_external_id": render_template(source_class.resolved_id_template(), row),
                            "in_external_id": render_template(target_class.resolved_id_template(), row),
                            "edge_external_id": render_template(
                                relationship.resolved_id_template(source_class, target_class),
                                row,
                            ),
                            "properties": edge_properties,
                        }
                    )

                result = janusgraph.upsert_edge_batch(
                    edge_label=relationship.resolved_edge_label(),
                    out_label=source_class.resolved_vertex_label(),
                    in_label=target_class.resolved_vertex_label(),
                    rows=payload,
                )
                stats["edges_loaded"] += result["processed"]
                stats["missing_vertices"] += result["missing"]

        if self.config.runtime.fail_on_missing_vertex and stats["missing_vertices"]:
            raise RuntimeError(
                f"Relationship '{relationship.iri}' had {stats['missing_vertices']} rows referencing missing vertices"
            )
        return stats

    def build_vertex_properties(
        self,
        ontology: OntologyCatalog,
        class_mapping: ClassMapping,
        row: Dict[str, Any],
        props: Sequence[PropertyMapping],
        batch_id: str,
        loaded_at: str,
    ) -> List[Dict[str, Any]]:
        class_key = class_mapping.source.key_column
        if not class_key:
            raise ValueError(f"Class '{class_mapping.iri}' does not define source.key_column")

        vertex_properties: List[Dict[str, Any]] = [
            {"key": "ontology_iri", "value": class_mapping.iri, "data_type": "String", "cardinality": "SINGLE"},
            {
                "key": "source_table",
                "value": class_mapping.source.table or "custom_sql",
                "data_type": "String",
                "cardinality": "SINGLE",
            },
            {
                "key": "source_primary_key",
                "value": str(row[class_key]),
                "data_type": "String",
                "cardinality": "SINGLE",
            },
            {"key": "load_batch_id", "value": batch_id, "data_type": "String", "cardinality": "SINGLE"},
            {"key": "loaded_at", "value": loaded_at, "data_type": "Instant", "cardinality": "SINGLE"},
        ]

        for ontology_type_iri in sorted(ontology.class_lineage(class_mapping.iri)):
            vertex_properties.append(
                {
                    "key": "rdf_types",
                    "value": ontology_type_iri,
                    "data_type": "String",
                    "cardinality": "SET",
                }
            )

        for prop in props:
            property_spec = self.build_single_property(ontology, prop, row)
            if property_spec is not None:
                vertex_properties.append(property_spec)
        return vertex_properties

    def build_single_property(
        self,
        ontology: OntologyCatalog,
        prop: PropertyMapping,
        row: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if not prop.source_column:
            raise ValueError(f"Property '{prop.iri}' requires source_column")
        if prop.source_column not in row:
            raise KeyError(
                f"Column '{prop.source_column}' is missing from query result for property '{prop.iri}'. "
                f"Available columns: {sorted(set(row.keys()))}"
            )
        value = row[prop.source_column]
        if value is None:
            return None

        data_type = normalize_data_type(
            prop.data_type or infer_data_type_from_ranges(ontology.properties.get(prop.iri, OntologyProperty(prop.iri, "unknown")).ranges)
        )
        return {
            "key": prop.resolved_property_key(),
            "value": coerce_value_for_transport(value, data_type),
            "data_type": data_type,
            "cardinality": prop.cardinality,
        }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load ontology-driven data from Teradata into JanusGraph")
    parser.add_argument("--mapping", required=True, help="Path to YAML or JSON mapping file")
    parser.add_argument("--ontology", help="Optional override for ontology file path")
    parser.add_argument("--dry-run", action="store_true", help="Validate config and print schema/SQL plan only")
    parser.add_argument("--strict-ontology", action="store_true", help="Fail if mappings reference unknown ontology terms")
    parser.add_argument("--report-file", help="Optional output path for JSON load report")
    parser.add_argument("--log-level", default="INFO", help="Logging level (DEBUG, INFO, WARNING, ERROR)")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(args.log_level)
    load_optional_dotenv()

    try:
        config = LoaderConfig.from_file(
            file_path=args.mapping,
            ontology_override=args.ontology,
            dry_run_override=args.dry_run,
            strict_override=True if args.strict_ontology else None,
        )
        loader = KnowledgeGraphLoader(config)
        report = loader.run()
        report_json = json.dumps(report, indent=2, sort_keys=True)
        if args.report_file:
            Path(args.report_file).write_text(report_json + "\n", encoding="utf-8")
            LOG.info("Wrote report to %s", args.report_file)
        print(report_json)
        return 0
    except Exception as exc:  # pragma: no cover - CLI safety net
        LOG.exception("Loader failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
