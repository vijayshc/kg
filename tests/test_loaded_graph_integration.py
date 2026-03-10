from __future__ import annotations

import os
import sys
import unittest
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from gremlin_python.driver.client import Client
from gremlin_python.driver.serializer import GraphSONSerializersV3d0


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import kg_loader


VERTEX_DYNAMIC_KEYS = {"load_batch_id", "loaded_at"}
EDGE_DYNAMIC_KEYS = {"load_batch_id", "loaded_at"}


@dataclass
class ExpectedProperty:
    data_type: str
    cardinality: str
    value: Any


@dataclass
class ExpectedVertex:
    external_id: str
    label: str
    properties: Dict[str, ExpectedProperty] = field(default_factory=dict)


@dataclass
class ExpectedEdge:
    edge_external_id: str
    label: str
    out_external_id: str
    in_external_id: str
    properties: Dict[str, ExpectedProperty] = field(default_factory=dict)


class GraphClient:
    def __init__(self, url: str, traversal_source: str) -> None:
        self._client = Client(url, traversal_source, message_serializer=GraphSONSerializersV3d0())

    def close(self) -> None:
        self._client.close()

    def submit_one(self, script: str, bindings: Dict[str, Any] | None = None) -> Any:
        results = self._client.submit(script, bindings=bindings or {}).all().result()
        return results[0] if results else None

    def ping(self) -> str:
        return str(self.submit_one("g.inject('ok')"))

    def count_vertices(self, label: str) -> int:
        return int(self.submit_one("g.V().hasLabel(vertexLabel).count()", {"vertexLabel": label}))

    def count_edges(self, label: str) -> int:
        return int(self.submit_one("g.E().hasLabel(edgeLabel).count()", {"edgeLabel": label}))

    def count_vertices_with_ontology(self, label: str, ontology_iri: str) -> int:
        return int(
            self.submit_one(
                "g.V().hasLabel(vertexLabel).has('ontology_iri', ontologyIri).count()",
                {"vertexLabel": label, "ontologyIri": ontology_iri},
            )
        )

    def count_edges_with_ontology(self, label: str, ontology_iri: str) -> int:
        return int(
            self.submit_one(
                "g.E().hasLabel(edgeLabel).has('ontology_iri', ontologyIri).count()",
                {"edgeLabel": label, "ontologyIri": ontology_iri},
            )
        )

    def count_vertices_missing_property(self, label: str, property_key: str) -> int:
        return int(
            self.submit_one(
                "g.V().hasLabel(vertexLabel).hasNot(propertyKey).count()",
                {"vertexLabel": label, "propertyKey": property_key},
            )
        )

    def count_edges_missing_property(self, label: str, property_key: str) -> int:
        return int(
            self.submit_one(
                "g.E().hasLabel(edgeLabel).hasNot(propertyKey).count()",
                {"edgeLabel": label, "propertyKey": property_key},
            )
        )

    def count_edges_with_end_labels(self, edge_label: str, out_label: str, in_label: str) -> int:
        return int(
            self.submit_one(
                "g.E().hasLabel(edgeLabel).where(outV().hasLabel(outLabel)).where(inV().hasLabel(inLabel)).count()",
                {"edgeLabel": edge_label, "outLabel": out_label, "inLabel": in_label},
            )
        )

    def fetch_vertex(self, external_id: str) -> Dict[str, Any] | None:
        return self.submit_one(
            "g.V().has('external_id', externalId).limit(1)"
            ".project('label','props')"
            ".by(label())"
            ".by(properties().group().by(key()).by(value().fold()))"
            ".fold().coalesce(unfold(), constant(null))",
            {"externalId": external_id},
        )

    def fetch_edge(self, edge_external_id: str) -> Dict[str, Any] | None:
        return self.submit_one(
            "g.E().has('edge_external_id', edgeExternalId).limit(1)"
            ".project('label','outExternalId','inExternalId','props')"
            ".by(label())"
            ".by(outV().values('external_id'))"
            ".by(inV().values('external_id'))"
            ".by(properties().group().by(key()).by(value().fold()))"
            ".fold().coalesce(unfold(), constant(null))",
            {"edgeExternalId": edge_external_id},
        )


class ExpectedGraphModelBuilder:
    def __init__(self, config: kg_loader.LoaderConfig, ontology: kg_loader.OntologyCatalog) -> None:
        self.config = config
        self.ontology = ontology
        self.loader = kg_loader.KnowledgeGraphLoader(config)
        self.data_client = self.loader.build_data_client()
        self.classes_by_iri = {item.iri: item for item in config.classes}

    def close(self) -> None:
        self.data_client.close()

    def build_expected_vertices(self) -> Dict[str, ExpectedVertex]:
        expected: Dict[str, ExpectedVertex] = {}
        for class_mapping in self.config.classes:
            inline_props, external_props = kg_loader.split_class_properties(class_mapping)
            fetch_size = class_mapping.source.fetch_size or self.config.runtime.batch_size

            for batch in self.data_client.iter_class_batches(class_mapping, inline_props, fetch_size):
                for row in batch:
                    external_id = kg_loader.render_template(class_mapping.resolved_id_template(), row)
                    record = expected.setdefault(
                        external_id,
                        ExpectedVertex(external_id=external_id, label=class_mapping.resolved_vertex_label()),
                    )
                    self._merge_property(record.properties, "external_id", external_id, "String", "SINGLE")
                    self._merge_property(record.properties, "ontology_iri", class_mapping.iri, "String", "SINGLE")
                    self._merge_property(
                        record.properties,
                        "source_table",
                        class_mapping.source.table or "custom_sql",
                        "String",
                        "SINGLE",
                    )
                    if not class_mapping.source.key_column:
                        raise ValueError(f"Class '{class_mapping.iri}' requires source.key_column")
                    self._merge_property(
                        record.properties,
                        "source_primary_key",
                        str(row[class_mapping.source.key_column]),
                        "String",
                        "SINGLE",
                    )
                    for prop in inline_props:
                        self._merge_vertex_property(record.properties, prop, row)

            for prop in external_props:
                for batch in self.data_client.iter_property_batches(class_mapping, prop, fetch_size):
                    for row in batch:
                        external_id = kg_loader.render_template(class_mapping.resolved_id_template(), row)
                        record = expected.setdefault(
                            external_id,
                            ExpectedVertex(external_id=external_id, label=class_mapping.resolved_vertex_label()),
                        )
                        self._merge_vertex_property(record.properties, prop, row)
        return expected

    def build_expected_edges(self) -> Dict[str, ExpectedEdge]:
        expected: Dict[str, ExpectedEdge] = {}
        for relationship in self.config.relationships:
            source_class = self.classes_by_iri[relationship.source_class_iri]
            target_class = self.classes_by_iri[relationship.target_class_iri]
            fetch_size = relationship.source.fetch_size or self.config.runtime.batch_size
            for batch in self.data_client.iter_relationship_batches(relationship, source_class, target_class, fetch_size):
                for row in batch:
                    out_external_id = kg_loader.render_template(source_class.resolved_id_template(), row)
                    in_external_id = kg_loader.render_template(target_class.resolved_id_template(), row)
                    edge_external_id = kg_loader.render_template(
                        relationship.resolved_id_template(source_class, target_class),
                        row,
                    )
                    record = expected.setdefault(
                        edge_external_id,
                        ExpectedEdge(
                            edge_external_id=edge_external_id,
                            label=relationship.resolved_edge_label(),
                            out_external_id=out_external_id,
                            in_external_id=in_external_id,
                        ),
                    )
                    self._merge_property(record.properties, "edge_external_id", edge_external_id, "String", "SINGLE")
                    self._merge_property(record.properties, "ontology_iri", relationship.iri, "String", "SINGLE")
                    self._merge_property(
                        record.properties,
                        "source_table",
                        relationship.source.table or "custom_sql",
                        "String",
                        "SINGLE",
                    )
                    for prop in relationship.properties:
                        self._merge_edge_property(record.properties, prop, row)
        return expected

    def _resolve_data_type(self, prop: kg_loader.PropertyMapping) -> str:
        ontology_property = self.ontology.properties.get(prop.iri)
        inferred = kg_loader.infer_data_type_from_ranges(ontology_property.ranges) if ontology_property else "String"
        return kg_loader.normalize_data_type(prop.data_type or inferred)

    def _merge_vertex_property(
        self,
        property_store: Dict[str, ExpectedProperty],
        prop: kg_loader.PropertyMapping,
        row: Dict[str, Any],
    ) -> None:
        if not prop.source_column or prop.source_column not in row:
            return
        value = row[prop.source_column]
        if value is None:
            return
        self._merge_property(
            property_store,
            prop.resolved_property_key(),
            value,
            self._resolve_data_type(prop),
            prop.cardinality,
        )

    def _merge_edge_property(
        self,
        property_store: Dict[str, ExpectedProperty],
        prop: kg_loader.PropertyMapping,
        row: Dict[str, Any],
    ) -> None:
        self._merge_vertex_property(property_store, prop, row)

    def _merge_property(
        self,
        property_store: Dict[str, ExpectedProperty],
        property_key: str,
        raw_value: Any,
        data_type: str,
        cardinality: str,
    ) -> None:
        if raw_value is None:
            return
        canonical_value = kg_loader.coerce_value_for_transport(raw_value, data_type)
        if cardinality == "SINGLE":
            property_store[property_key] = ExpectedProperty(data_type=data_type, cardinality=cardinality, value=canonical_value)
            return

        existing = property_store.get(property_key)
        values = list(existing.value) if existing else []
        if cardinality == "SET":
            if canonical_value not in values:
                values.append(canonical_value)
        else:
            values.append(canonical_value)
        property_store[property_key] = ExpectedProperty(data_type=data_type, cardinality=cardinality, value=values)


def normalize_graph_scalar(value: Any, data_type: str) -> Any:
    normalized_type = kg_loader.normalize_data_type(data_type)
    if normalized_type == "Date":
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, dict) and "@value" in value:
            return str(value["@value"])[:10]
        return str(value)[:10]

    if normalized_type == "Instant":
        if isinstance(value, dict) and "@value" in value:
            return str(value["@value"])
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return str(value)

    if normalized_type in {"Integer", "Long"}:
        return int(value)

    if normalized_type in {"Float", "Double", "Decimal"}:
        return float(value)

    if normalized_type == "Boolean":
        return value if isinstance(value, bool) else kg_loader.as_bool(value)

    return str(value) if not isinstance(value, str) else value


def normalize_graph_property(raw_values: Any, expected_property: ExpectedProperty) -> Any:
    if raw_values is None:
        return None
    values = raw_values if isinstance(raw_values, list) else [raw_values]
    normalized_values = [normalize_graph_scalar(value, expected_property.data_type) for value in values]
    if expected_property.cardinality == "SINGLE":
        return normalized_values[0] if normalized_values else None
    if expected_property.cardinality == "SET":
        return sorted(set(normalized_values))
    return normalized_values


class LoadedGraphIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("JANUSGRAPH_URL", "ws://127.0.0.1:8182/gremlin")
        os.environ.setdefault("JANUSGRAPH_TRAVERSAL_SOURCE", "g")

        cls.mapping_path = ROOT / "config" / "ontology_mapping.test.yaml"
        cls.config = kg_loader.LoaderConfig.from_file(str(cls.mapping_path))
        cls.ontology = kg_loader.load_ontology(cls.config.ontology)

        cls.graph = GraphClient(cls.config.janusgraph.url, cls.config.janusgraph.traversal_source)
        try:
            if cls.graph.ping() != "ok":
                raise RuntimeError("Unexpected JanusGraph ping response")
        except Exception as exc:  # pragma: no cover - environment-specific failure
            raise RuntimeError(
                "JanusGraph is not reachable for integration tests. "
                "Start it with 'bash scripts/run_local_test_mode.sh' or 'bash scripts/start_janusgraph_test.sh'."
            ) from exc

        cls.builder = ExpectedGraphModelBuilder(cls.config, cls.ontology)
        cls.expected_vertices = cls.builder.build_expected_vertices()
        cls.expected_edges = cls.builder.build_expected_edges()
        cls.expected_vertex_counts = Counter(item.label for item in cls.expected_vertices.values())
        cls.expected_edge_counts = Counter(item.label for item in cls.expected_edges.values())
        cls.class_mappings_by_label = {item.resolved_vertex_label(): item for item in cls.config.classes}
        cls.relationship_mappings_by_label = {item.resolved_edge_label(): item for item in cls.config.relationships}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.builder.close()
        cls.graph.close()

    def test_janusgraph_is_reachable(self) -> None:
        self.assertEqual(self.graph.ping(), "ok")

    def test_vertex_counts_match_input_data(self) -> None:
        for label, expected_count in sorted(self.expected_vertex_counts.items()):
            with self.subTest(label=label):
                self.assertEqual(self.graph.count_vertices(label), expected_count)

    def test_edge_counts_match_input_data(self) -> None:
        for label, expected_count in sorted(self.expected_edge_counts.items()):
            with self.subTest(label=label):
                self.assertEqual(self.graph.count_edges(label), expected_count)

    def test_vertex_ontology_metadata_matches_mapping(self) -> None:
        for label, class_mapping in sorted(self.class_mappings_by_label.items()):
            with self.subTest(label=label):
                expected_count = self.expected_vertex_counts[label]
                actual_count = self.graph.count_vertices_with_ontology(label, class_mapping.iri)
                self.assertEqual(actual_count, expected_count)

    def test_edge_ontology_metadata_matches_mapping(self) -> None:
        for label, relationship_mapping in sorted(self.relationship_mappings_by_label.items()):
            with self.subTest(label=label):
                expected_count = self.expected_edge_counts[label]
                actual_count = self.graph.count_edges_with_ontology(label, relationship_mapping.iri)
                self.assertEqual(actual_count, expected_count)

    def test_all_vertices_have_required_load_metadata(self) -> None:
        for label in sorted(self.expected_vertex_counts):
            with self.subTest(label=label, property_key="load_batch_id"):
                self.assertEqual(self.graph.count_vertices_missing_property(label, "load_batch_id"), 0)
            with self.subTest(label=label, property_key="loaded_at"):
                self.assertEqual(self.graph.count_vertices_missing_property(label, "loaded_at"), 0)

    def test_all_edges_have_required_load_metadata(self) -> None:
        for label in sorted(self.expected_edge_counts):
            with self.subTest(label=label, property_key="load_batch_id"):
                self.assertEqual(self.graph.count_edges_missing_property(label, "load_batch_id"), 0)
            with self.subTest(label=label, property_key="loaded_at"):
                self.assertEqual(self.graph.count_edges_missing_property(label, "loaded_at"), 0)

    def test_each_expected_vertex_has_expected_properties(self) -> None:
        for external_id, expected_vertex in sorted(self.expected_vertices.items()):
            with self.subTest(external_id=external_id):
                actual_vertex = self.graph.fetch_vertex(external_id)
                self.assertIsNotNone(actual_vertex, f"Missing vertex '{external_id}'")
                self.assertEqual(actual_vertex["label"], expected_vertex.label)
                actual_properties = actual_vertex["props"]

                for property_key, expected_property in expected_vertex.properties.items():
                    with self.subTest(external_id=external_id, property_key=property_key):
                        self.assertIn(property_key, actual_properties, f"Missing property '{property_key}' on vertex '{external_id}'")
                        actual_value = normalize_graph_property(actual_properties[property_key], expected_property)
                        if expected_property.data_type == "Decimal":
                            self.assertAlmostEqual(float(actual_value), float(expected_property.value), places=6)
                        else:
                            self.assertEqual(actual_value, expected_property.value)

                for dynamic_key in VERTEX_DYNAMIC_KEYS:
                    self.assertIn(dynamic_key, actual_properties)

    def test_customer_external_properties_from_secondary_csv_tables(self) -> None:
        customer_checks = {
            "Customer:C001": {"city": "Charlotte", "state": "NC", "country": "USA", "mobilePhone": "+1-704-555-0101"},
            "Customer:C002": {"city": "Atlanta", "state": "GA", "country": "USA", "mobilePhone": "+1-404-555-0102"},
            "Customer:C003": {"city": "Chicago", "state": "IL", "country": "USA", "mobilePhone": "+1-312-555-0103"},
        }
        for external_id, expected_values in customer_checks.items():
            with self.subTest(external_id=external_id):
                actual_vertex = self.graph.fetch_vertex(external_id)
                self.assertIsNotNone(actual_vertex)
                actual_properties = actual_vertex["props"]
                for property_key, expected_value in expected_values.items():
                    self.assertIn(property_key, actual_properties)
                    actual_value = normalize_graph_property(
                        actual_properties[property_key],
                        ExpectedProperty(data_type="String", cardinality="SINGLE", value=expected_value),
                    )
                    self.assertEqual(actual_value, expected_value)

    def test_each_expected_edge_has_expected_endpoints_and_properties(self) -> None:
        for edge_external_id, expected_edge in sorted(self.expected_edges.items()):
            with self.subTest(edge_external_id=edge_external_id):
                actual_edge = self.graph.fetch_edge(edge_external_id)
                self.assertIsNotNone(actual_edge, f"Missing edge '{edge_external_id}'")
                self.assertEqual(actual_edge["label"], expected_edge.label)
                self.assertEqual(actual_edge["outExternalId"], expected_edge.out_external_id)
                self.assertEqual(actual_edge["inExternalId"], expected_edge.in_external_id)
                actual_properties = actual_edge["props"]

                for property_key, expected_property in expected_edge.properties.items():
                    with self.subTest(edge_external_id=edge_external_id, property_key=property_key):
                        self.assertIn(property_key, actual_properties, f"Missing property '{property_key}' on edge '{edge_external_id}'")
                        actual_value = normalize_graph_property(actual_properties[property_key], expected_property)
                        if expected_property.data_type == "Decimal":
                            self.assertAlmostEqual(float(actual_value), float(expected_property.value), places=6)
                        else:
                            self.assertEqual(actual_value, expected_property.value)

                for dynamic_key in EDGE_DYNAMIC_KEYS:
                    self.assertIn(dynamic_key, actual_properties)

    def test_edge_domain_and_range_labels_match_ontology_mapping(self) -> None:
        class_labels_by_iri = {item.iri: item.resolved_vertex_label() for item in self.config.classes}
        for relationship in self.config.relationships:
            edge_label = relationship.resolved_edge_label()
            out_label = class_labels_by_iri[relationship.source_class_iri]
            in_label = class_labels_by_iri[relationship.target_class_iri]
            expected_count = self.expected_edge_counts[edge_label]
            with self.subTest(edge_label=edge_label):
                actual_count = self.graph.count_edges_with_end_labels(edge_label, out_label, in_label)
                self.assertEqual(actual_count, expected_count)


if __name__ == "__main__":
    unittest.main(verbosity=2)
