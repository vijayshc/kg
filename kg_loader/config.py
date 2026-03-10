from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .constants import VALID_MODES, VALID_SOURCE_TYPES
from .utils import (
    as_bool,
    extract_template_fields,
    local_name,
    normalize_cardinality,
    normalize_multiplicity,
    safe_name,
    substitute_env_values,
    unique_preserve_order,
    validate_gremlin_identifier,
    validate_sql_fragment,
    validate_sql_statement,
    validate_websocket_url,
)


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
