from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from .config import ClassMapping, LoaderConfig, PropertyMapping, RelationshipMapping, split_class_properties
from .data_clients import CsvClient, TabularDataClient, TeradataClient
from .gremlin import JanusGraphClient
from .ontology import OntologyCatalog, OntologyProperty, load_ontology, validate_mapping_against_ontology
from .schema import SchemaPlanner
from .utils import (
    chunked,
    coerce_value_for_transport,
    encode_sorted_string_set,
    infer_data_type_from_ranges,
    normalize_data_type,
    render_template,
    to_iso_instant,
)


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
        try:
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
                janusgraph.close()
        finally:
            data_client.close()

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
        property_lineage = encode_sorted_string_set(ontology.property_lineage(relationship.iri))
        inverse_properties = encode_sorted_string_set(ontology.inverse_properties_for(relationship.iri))

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
