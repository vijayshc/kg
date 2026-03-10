from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional, Tuple

from .config import LoaderConfig, PropertyMapping
from .constants import SYSTEM_EDGE_PROPERTY_SPECS, SYSTEM_VERTEX_PROPERTY_SPECS
from .ontology import OntologyCatalog
from .utils import infer_data_type_from_ranges, local_name, normalize_cardinality, normalize_data_type, normalize_multiplicity, safe_name


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
