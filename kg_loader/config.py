from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .constants import (
    SYSTEM_EDGE_PROPERTY_SPECS,
    SYSTEM_VERTEX_PROPERTY_SPECS,
    VALID_INDEX_ACTIVATION_MODES,
    VALID_INDEX_ELEMENTS,
    VALID_INDEX_KINDS,
    VALID_QUERY_PATTERN_TYPES,
    VALID_MODES,
    VALID_SOURCE_TYPES,
)
from .utils import (
    as_bool,
    extract_template_fields,
    local_name,
    normalize_cardinality,
    normalize_data_type,
    normalize_index_mapping,
    normalize_multiplicity,
    normalize_query_operator,
    normalize_relation_direction,
    normalize_sort_order,
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
    meta_properties: List["MetaPropertyMapping"] = field(default_factory=list)

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
            meta_properties=[MetaPropertyMapping.from_dict(item) for item in raw.get("meta_properties", [])],
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
class MetaPropertyMapping:
    property_key: str
    source_column: Optional[str] = None
    constant_value: Any = None
    data_type: str = "String"

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "MetaPropertyMapping":
        return cls(
            property_key=str(raw["property_key"]),
            source_column=raw.get("source_column"),
            constant_value=raw.get("constant_value"),
            data_type=normalize_data_type(raw.get("data_type") or "String"),
        )


@dataclass
class IndexKeyOptions:
    property_key: str
    mapping: Optional[str] = None
    mapped_name: Optional[str] = None
    text_analyzer: Optional[str] = None
    string_analyzer: Optional[str] = None
    custom_parameters: Dict[str, Any] = field(default_factory=dict)
    geo_max_levels: Optional[int] = None
    geo_dist_error_pct: Optional[float] = None

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "IndexKeyOptions":
        property_key = raw.get("property_key") or raw.get("key")
        if not property_key:
            raise ValueError("Index key definitions require property_key")
        return cls(
            property_key=str(property_key),
            mapping=normalize_index_mapping(raw.get("mapping")),
            mapped_name=raw.get("mapped_name"),
            text_analyzer=raw.get("text_analyzer"),
            string_analyzer=raw.get("string_analyzer"),
            custom_parameters=dict(raw.get("custom_parameters") or {}),
            geo_max_levels=int(raw["geo_max_levels"]) if raw.get("geo_max_levels") is not None else None,
            geo_dist_error_pct=(
                float(raw["geo_dist_error_pct"]) if raw.get("geo_dist_error_pct") is not None else None
            ),
        )

    def has_parameters(self) -> bool:
        return any(
            value is not None and value != {}
            for value in (
                self.mapping,
                self.mapped_name,
                self.text_analyzer,
                self.string_analyzer,
                self.custom_parameters,
                self.geo_max_levels,
                self.geo_dist_error_pct,
            )
        )


@dataclass
class GraphIndexDefinition:
    name: str
    element: str
    property_keys: List[str]
    kind: str = "composite"
    unique: bool = False
    backend: Optional[str] = None
    index_only: Optional[str] = None
    key_options: Dict[str, IndexKeyOptions] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "GraphIndexDefinition":
        property_keys: List[str] = []
        key_options: Dict[str, IndexKeyOptions] = {}

        keys_raw = raw.get("keys")
        if keys_raw is not None:
            for item in list(keys_raw or []):
                if isinstance(item, dict):
                    key_spec = IndexKeyOptions.from_dict(item)
                    property_keys.append(key_spec.property_key)
                    key_options[key_spec.property_key] = key_spec
                else:
                    property_keys.append(str(item))
        else:
            property_keys_raw = raw.get("property_keys")
            if isinstance(property_keys_raw, str):
                property_keys = [property_keys_raw]
            else:
                property_keys = [str(item) for item in list(property_keys_raw or [])]

        return cls(
            name=raw["name"],
            element=str(raw.get("element", "vertex")).lower(),
            property_keys=[str(item) for item in property_keys],
            kind=str(raw.get("kind", "composite")).lower(),
            unique=as_bool(raw.get("unique"), False),
            backend=raw.get("backend"),
            index_only=raw.get("index_only"),
            key_options=key_options,
        )

    def ordered_key_options(self) -> List[IndexKeyOptions]:
        return [self.key_options.get(property_key, IndexKeyOptions(property_key=property_key)) for property_key in self.property_keys]


@dataclass
class EdgeRelationIndexDefinition:
    name: str
    edge_label: str
    property_keys: List[str]
    direction: str = "BOTH"
    sort_order: str = "asc"

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "EdgeRelationIndexDefinition":
        property_keys_raw = raw.get("property_keys")
        if isinstance(property_keys_raw, str):
            property_keys = [property_keys_raw]
        else:
            property_keys = [str(item) for item in list(property_keys_raw or [])]
        return cls(
            name=raw["name"],
            edge_label=str(raw["edge_label"]),
            property_keys=property_keys,
            direction=normalize_relation_direction(raw.get("direction")),
            sort_order=normalize_sort_order(raw.get("sort_order")),
        )


@dataclass
class PropertyRelationIndexDefinition:
    name: str
    property_key: str
    meta_property_keys: List[str]
    sort_order: str = "asc"

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "PropertyRelationIndexDefinition":
        meta_keys_raw = raw.get("meta_property_keys")
        if isinstance(meta_keys_raw, str):
            meta_property_keys = [meta_keys_raw]
        else:
            meta_property_keys = [str(item) for item in list(meta_keys_raw or [])]
        return cls(
            name=raw["name"],
            property_key=str(raw["property_key"]),
            meta_property_keys=meta_property_keys,
            sort_order=normalize_sort_order(raw.get("sort_order")),
        )


@dataclass
class QueryPredicateDefinition:
    property_key: str
    operator: str = "eq"

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "QueryPredicateDefinition":
        return cls(
            property_key=str(raw["property_key"]),
            operator=normalize_query_operator(raw.get("operator")),
        )


@dataclass
class QueryPatternDefinition:
    name: str
    pattern_type: str = "graph"
    element: str = "vertex"
    label: Optional[str] = None
    start_label: Optional[str] = None
    edge_label: Optional[str] = None
    direction: str = "OUT"
    property_key: Optional[str] = None
    predicates: List[QueryPredicateDefinition] = field(default_factory=list)
    order_by: Optional[str] = None
    order: str = "asc"
    limit: Optional[int] = None

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "QueryPatternDefinition":
        return cls(
            name=str(raw["name"]),
            pattern_type=str(raw.get("type", "graph")).lower(),
            element=str(raw.get("element", "vertex")).lower(),
            label=raw.get("label"),
            start_label=raw.get("start_label"),
            edge_label=raw.get("edge_label"),
            direction=normalize_relation_direction(raw.get("direction") or "OUT"),
            property_key=raw.get("property_key"),
            predicates=[QueryPredicateDefinition.from_dict(item) for item in raw.get("predicates", [])],
            order_by=raw.get("order_by"),
            order=normalize_sort_order(raw.get("order")),
            limit=int(raw["limit"]) if raw.get("limit") is not None else None,
        )


@dataclass
class JanusGraphSettings:
    url: str
    traversal_source: str = "g"
    graph_alias: str = "graph"
    create_schema: bool = True
    create_schema_constraints: bool = True
    request_timeout_seconds: int = 300
    index_activation_mode: str = "reindex"
    index_reindex_concurrency: int = 0
    relation_index_activation_mode: str = "reindex"
    relation_index_reindex_concurrency: int = 0
    emit_query_pattern_recommendations: bool = True


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
    mapping_file: str = ""
    indexes: List[GraphIndexDefinition] = field(default_factory=list)
    edge_relation_indexes: List[EdgeRelationIndexDefinition] = field(default_factory=list)
    property_relation_indexes: List[PropertyRelationIndexDefinition] = field(default_factory=list)
    query_patterns: List[QueryPatternDefinition] = field(default_factory=list)

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
        relation_indexes_raw = raw.get("vertex_centric_indexes") or raw.get("relation_indexes") or {}

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
                create_schema_constraints=as_bool(janus_raw.get("create_schema_constraints", True), True),
                request_timeout_seconds=int(
                    janus_raw.get("request_timeout_seconds")
                    or os.getenv("JANUSGRAPH_REQUEST_TIMEOUT_SECONDS", 300)
                ),
                index_activation_mode=str(
                    janus_raw.get("index_activation_mode")
                    or os.getenv("JANUSGRAPH_INDEX_ACTIVATION_MODE", "reindex")
                ).lower(),
                index_reindex_concurrency=int(
                    janus_raw.get("index_reindex_concurrency")
                    or os.getenv("JANUSGRAPH_INDEX_REINDEX_CONCURRENCY", 0)
                ),
                relation_index_activation_mode=str(
                    janus_raw.get("relation_index_activation_mode")
                    or os.getenv("JANUSGRAPH_RELATION_INDEX_ACTIVATION_MODE", "reindex")
                ).lower(),
                relation_index_reindex_concurrency=int(
                    janus_raw.get("relation_index_reindex_concurrency")
                    or os.getenv("JANUSGRAPH_RELATION_INDEX_REINDEX_CONCURRENCY", 0)
                ),
                emit_query_pattern_recommendations=as_bool(
                    janus_raw.get("emit_query_pattern_recommendations", True),
                    True,
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
            indexes=[GraphIndexDefinition.from_dict(item) for item in raw.get("indexes", [])],
            edge_relation_indexes=[
                EdgeRelationIndexDefinition.from_dict(item) for item in relation_indexes_raw.get("edge_indexes", [])
            ],
            property_relation_indexes=[
                PropertyRelationIndexDefinition.from_dict(item)
                for item in relation_indexes_raw.get("property_indexes", [])
            ],
            query_patterns=[QueryPatternDefinition.from_dict(item) for item in raw.get("query_patterns", [])],
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
        if self.janusgraph.index_activation_mode not in VALID_INDEX_ACTIVATION_MODES:
            raise ValueError(
                f"Unsupported JanusGraph index_activation_mode '{self.janusgraph.index_activation_mode}'. "
                f"Supported: {sorted(VALID_INDEX_ACTIVATION_MODES)}"
            )
        if self.janusgraph.index_reindex_concurrency < 0:
            raise ValueError("JanusGraph index_reindex_concurrency must be >= 0")
        if self.janusgraph.relation_index_activation_mode not in VALID_INDEX_ACTIVATION_MODES:
            raise ValueError(
                f"Unsupported JanusGraph relation_index_activation_mode '{self.janusgraph.relation_index_activation_mode}'. "
                f"Supported: {sorted(VALID_INDEX_ACTIVATION_MODES)}"
            )
        if self.janusgraph.relation_index_reindex_concurrency < 0:
            raise ValueError("JanusGraph relation_index_reindex_concurrency must be >= 0")
        if not self.runtime.dry_run:
            validate_websocket_url(self.janusgraph.url)

        class_iris = set()
        vertex_labels = set()
        vertex_property_keys = {spec["name"] for spec in SYSTEM_VERTEX_PROPERTY_SPECS}
        edge_property_keys = {spec["name"] for spec in SYSTEM_EDGE_PROPERTY_SPECS}
        global_property_keys = set(vertex_property_keys) | set(edge_property_keys)
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
                vertex_property_keys.add(prop.resolved_property_key())
                global_property_keys.add(prop.resolved_property_key())
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
                for meta_prop in prop.meta_properties:
                    global_property_keys.add(meta_prop.property_key)
                    if bool(meta_prop.source_column) == (meta_prop.constant_value is not None):
                        raise ValueError(
                            f"Meta property '{meta_prop.property_key}' on '{prop.iri}' must define exactly one of "
                            "source_column or constant_value"
                        )
                    if meta_prop.source_column:
                        validate_sql_fragment(
                            meta_prop.source_column,
                            f"Meta property '{meta_prop.property_key}' source_column",
                        )

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
                edge_property_keys.add(prop.resolved_property_key())
                global_property_keys.add(prop.resolved_property_key())
                validate_sql_fragment(prop.source_column, f"Relationship property '{prop.iri}' source_column")
                if prop.meta_properties:
                    raise ValueError(
                        f"Relationship property '{prop.iri}' cannot define meta_properties because JanusGraph meta-properties "
                        "apply to vertex properties, not edge properties"
                    )

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

        configured_index_names: set[str] = set()
        for index in self.indexes:
            validate_gremlin_identifier(index.name, "Index name")
            if index.name in configured_index_names:
                raise ValueError(f"Duplicate index definition '{index.name}'")
            configured_index_names.add(index.name)

            if index.element not in VALID_INDEX_ELEMENTS:
                raise ValueError(
                    f"Index '{index.name}' has unsupported element '{index.element}'. "
                    f"Supported: {sorted(VALID_INDEX_ELEMENTS)}"
                )
            if index.kind not in VALID_INDEX_KINDS:
                raise ValueError(
                    f"Index '{index.name}' has unsupported kind '{index.kind}'. "
                    f"Supported: {sorted(VALID_INDEX_KINDS)}"
                )
            if not index.property_keys:
                raise ValueError(f"Index '{index.name}' must declare at least one property key")
            if index.kind == "mixed" and not index.backend:
                raise ValueError(f"Mixed index '{index.name}' requires backend")
            if index.kind == "mixed" and index.unique:
                raise ValueError(f"Mixed index '{index.name}' cannot be unique in JanusGraph")
            if index.element == "edge" and index.unique:
                raise ValueError(f"Edge index '{index.name}' cannot be unique in JanusGraph")

            known_property_keys = vertex_property_keys if index.element == "vertex" else edge_property_keys
            missing_keys = sorted(set(index.property_keys) - known_property_keys)
            if missing_keys:
                raise ValueError(
                    f"Index '{index.name}' references unknown property key(s) {missing_keys}. "
                    f"Known keys for {index.element} indexes: {sorted(known_property_keys)}"
                )

            if index.index_only:
                known_labels = vertex_labels if index.element == "vertex" else relationship_labels
                if index.index_only not in known_labels:
                    raise ValueError(
                        f"Index '{index.name}' references unknown label '{index.index_only}' in index_only. "
                        f"Known {index.element} labels: {sorted(known_labels)}"
                    )

            for property_key, options in index.key_options.items():
                if property_key not in index.property_keys:
                    raise ValueError(
                        f"Index '{index.name}' declares key options for unknown property key '{property_key}'"
                    )
                if index.kind != "mixed" and options.has_parameters():
                    raise ValueError(
                        f"Index '{index.name}' uses advanced key parameters for '{property_key}', but those are only "
                        "supported on mixed indexes"
                    )
                if options.geo_max_levels is not None or options.geo_dist_error_pct is not None:
                    if options.mapping != "PREFIX_TREE":
                        raise ValueError(
                            f"Index '{index.name}' can only use geo prefix-tree parameters on keys mapped as PREFIX_TREE"
                        )

        relation_index_names = set(configured_index_names)
        for index in self.edge_relation_indexes:
            validate_gremlin_identifier(index.name, "Relation index name")
            if index.name in relation_index_names:
                raise ValueError(f"Duplicate relation index definition '{index.name}'")
            relation_index_names.add(index.name)
            if not index.property_keys:
                raise ValueError(f"Edge relation index '{index.name}' must declare at least one property key")
            if index.edge_label not in relationship_labels:
                raise ValueError(
                    f"Edge relation index '{index.name}' references unknown edge label '{index.edge_label}'"
                )
            missing_keys = sorted(set(index.property_keys) - edge_property_keys)
            if missing_keys:
                raise ValueError(
                    f"Edge relation index '{index.name}' references unknown edge property key(s) {missing_keys}. "
                    f"Known edge keys: {sorted(edge_property_keys)}"
                )

        for index in self.property_relation_indexes:
            validate_gremlin_identifier(index.name, "Relation index name")
            if index.name in relation_index_names:
                raise ValueError(f"Duplicate relation index definition '{index.name}'")
            relation_index_names.add(index.name)
            if index.property_key not in vertex_property_keys:
                raise ValueError(
                    f"Property relation index '{index.name}' references unknown vertex property '{index.property_key}'"
                )
            if not index.meta_property_keys:
                raise ValueError(
                    f"Property relation index '{index.name}' must declare at least one meta_property_key"
                )
            missing_keys = sorted(set(index.meta_property_keys) - global_property_keys)
            if missing_keys:
                raise ValueError(
                    f"Property relation index '{index.name}' references unknown meta property key(s) {missing_keys}. "
                    f"Known property keys: {sorted(global_property_keys)}"
                )

        query_pattern_names: set[str] = set()
        for pattern in self.query_patterns:
            if pattern.name in query_pattern_names:
                raise ValueError(f"Duplicate query pattern '{pattern.name}'")
            query_pattern_names.add(pattern.name)
            if pattern.pattern_type not in VALID_QUERY_PATTERN_TYPES:
                raise ValueError(
                    f"Unsupported query pattern type '{pattern.pattern_type}'. Supported: {sorted(VALID_QUERY_PATTERN_TYPES)}"
                )

            if pattern.pattern_type == "graph":
                if pattern.element not in VALID_INDEX_ELEMENTS:
                    raise ValueError(
                        f"Graph query pattern '{pattern.name}' has unsupported element '{pattern.element}'. "
                        f"Supported: {sorted(VALID_INDEX_ELEMENTS)}"
                    )
                known_keys = vertex_property_keys if pattern.element == "vertex" else edge_property_keys
                if pattern.label:
                    known_labels = vertex_labels if pattern.element == "vertex" else relationship_labels
                    if pattern.label not in known_labels:
                        raise ValueError(
                            f"Query pattern '{pattern.name}' references unknown label '{pattern.label}' for element "
                            f"'{pattern.element}'"
                        )
                if not pattern.predicates:
                    raise ValueError(f"Graph query pattern '{pattern.name}' must declare at least one predicate")
                missing_keys = sorted({item.property_key for item in pattern.predicates} - known_keys)
                if missing_keys:
                    raise ValueError(
                        f"Graph query pattern '{pattern.name}' references unknown property key(s) {missing_keys}"
                    )
                if pattern.order_by and pattern.order_by not in known_keys:
                    raise ValueError(
                        f"Graph query pattern '{pattern.name}' references unknown order_by key '{pattern.order_by}'"
                    )

            elif pattern.pattern_type == "traversal":
                if not pattern.start_label or pattern.start_label not in vertex_labels:
                    raise ValueError(
                        f"Traversal query pattern '{pattern.name}' references unknown start_label '{pattern.start_label}'"
                    )
                if not pattern.edge_label or pattern.edge_label not in relationship_labels:
                    raise ValueError(
                        f"Traversal query pattern '{pattern.name}' references unknown edge_label '{pattern.edge_label}'"
                    )
                if not pattern.predicates and not pattern.order_by:
                    raise ValueError(
                        f"Traversal query pattern '{pattern.name}' must declare at least one predicate or order_by key"
                    )
                missing_keys = sorted({item.property_key for item in pattern.predicates} - edge_property_keys)
                if missing_keys:
                    raise ValueError(
                        f"Traversal query pattern '{pattern.name}' references unknown edge property key(s) {missing_keys}"
                    )
                if pattern.order_by and pattern.order_by not in edge_property_keys:
                    raise ValueError(
                        f"Traversal query pattern '{pattern.name}' references unknown order_by key '{pattern.order_by}'"
                    )

            else:
                if not pattern.property_key or pattern.property_key not in vertex_property_keys:
                    raise ValueError(
                        f"Property-meta query pattern '{pattern.name}' references unknown property_key '{pattern.property_key}'"
                    )
                if not pattern.predicates and not pattern.order_by:
                    raise ValueError(
                        f"Property-meta query pattern '{pattern.name}' must declare at least one predicate or order_by key"
                    )
                missing_keys = sorted({item.property_key for item in pattern.predicates} - global_property_keys)
                if missing_keys:
                    raise ValueError(
                        f"Property-meta query pattern '{pattern.name}' references unknown meta property key(s) {missing_keys}"
                    )
                if pattern.order_by and pattern.order_by not in global_property_keys:
                    raise ValueError(
                        f"Property-meta query pattern '{pattern.name}' references unknown order_by key '{pattern.order_by}'"
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
