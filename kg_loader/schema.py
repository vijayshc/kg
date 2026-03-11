from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

from .config import GraphIndexDefinition, LoaderConfig, PropertyMapping
from .constants import SYSTEM_EDGE_PROPERTY_SPECS, SYSTEM_VERTEX_PROPERTY_SPECS
from .ontology import OntologyCatalog
from .utils import (
    default_relation_index_sort_order,
    infer_data_type_from_ranges,
    local_name,
    normalize_cardinality,
    normalize_data_type,
    normalize_multiplicity,
    safe_name,
    supports_auto_graph_index,
    supports_relation_index_data_type,
    unique_preserve_order,
)


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
class GraphIndexKeySpec:
    property_key: str
    mapping: Optional[str] = None
    mapped_name: Optional[str] = None
    text_analyzer: Optional[str] = None
    string_analyzer: Optional[str] = None
    custom_parameters: Dict[str, Any] = field(default_factory=dict)
    geo_max_levels: Optional[int] = None
    geo_dist_error_pct: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"property_key": self.property_key}
        if self.mapping:
            payload["mapping"] = self.mapping
        if self.mapped_name:
            payload["mapped_name"] = self.mapped_name
        if self.text_analyzer:
            payload["text_analyzer"] = self.text_analyzer
        if self.string_analyzer:
            payload["string_analyzer"] = self.string_analyzer
        if self.custom_parameters:
            payload["custom_parameters"] = dict(self.custom_parameters)
        if self.geo_max_levels is not None:
            payload["geo_max_levels"] = self.geo_max_levels
        if self.geo_dist_error_pct is not None:
            payload["geo_dist_error_pct"] = self.geo_dist_error_pct
        return payload


@dataclass
class IndexSpec:
    name: str
    key_specs: Tuple[GraphIndexKeySpec, ...]
    unique: bool
    kind: str = "composite"
    backend: Optional[str] = None
    index_only: Optional[str] = None

    @property
    def property_keys(self) -> Tuple[str, ...]:
        return tuple(item.property_key for item in self.key_specs)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "name": self.name,
            "property_keys": list(self.property_keys),
            "keys": [item.to_dict() for item in self.key_specs],
            "unique": self.unique,
            "kind": self.kind,
        }
        if len(self.property_keys) == 1:
            payload["property_key"] = self.property_keys[0]
        if self.backend:
            payload["backend"] = self.backend
        if self.index_only:
            payload["index_only"] = self.index_only
        return payload


@dataclass
class PropertyConstraintSpec:
    label: str
    property_keys: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {"label": self.label, "property_keys": list(self.property_keys)}


@dataclass
class ConnectionConstraintSpec:
    edge_label: str
    out_label: str
    in_label: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "edge_label": self.edge_label,
            "out_label": self.out_label,
            "in_label": self.in_label,
        }


@dataclass
class EdgeRelationIndexSpec:
    name: str
    edge_label: str
    direction: str
    sort_order: str
    property_keys: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "edge_label": self.edge_label,
            "direction": self.direction,
            "sort_order": self.sort_order,
            "property_keys": list(self.property_keys),
        }


@dataclass
class PropertyRelationIndexSpec:
    name: str
    property_key: str
    sort_order: str
    meta_property_keys: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "property_key": self.property_key,
            "sort_order": self.sort_order,
            "meta_property_keys": list(self.meta_property_keys),
        }


@dataclass
class SchemaPlan:
    property_keys: Dict[str, PropertyKeySpec] = field(default_factory=dict)
    vertex_labels: Dict[str, VertexLabelSpec] = field(default_factory=dict)
    edge_labels: Dict[str, EdgeLabelSpec] = field(default_factory=dict)
    vertex_indexes: Dict[str, IndexSpec] = field(default_factory=dict)
    edge_indexes: Dict[str, IndexSpec] = field(default_factory=dict)
    vertex_property_constraints: Dict[str, PropertyConstraintSpec] = field(default_factory=dict)
    edge_property_constraints: Dict[str, PropertyConstraintSpec] = field(default_factory=dict)
    connection_constraints: Dict[str, ConnectionConstraintSpec] = field(default_factory=dict)
    edge_relation_indexes: Dict[str, EdgeRelationIndexSpec] = field(default_factory=dict)
    property_relation_indexes: Dict[str, PropertyRelationIndexSpec] = field(default_factory=dict)

    def ensure_property_key(self, name: str, data_type: str, cardinality: str) -> None:
        existing = self.property_keys.get(name)
        spec = PropertyKeySpec(
            name=name,
            data_type=normalize_data_type(data_type),
            cardinality=normalize_cardinality(cardinality),
        )
        if existing and (existing.data_type != spec.data_type or existing.cardinality != spec.cardinality):
            raise ValueError(
                f"Property key '{name}' has conflicting definitions: existing={existing}, requested={spec}"
            )
        self.property_keys[name] = spec

    def ensure_vertex_label(self, name: str, ontology_iri: Optional[str] = None) -> None:
        self.vertex_labels[name] = VertexLabelSpec(name=name, ontology_iri=ontology_iri)

    def ensure_edge_label(self, name: str, multiplicity: str, ontology_iri: Optional[str] = None) -> None:
        existing = self.edge_labels.get(name)
        spec = EdgeLabelSpec(name=name, multiplicity=normalize_multiplicity(multiplicity), ontology_iri=ontology_iri)
        if existing and existing.multiplicity != spec.multiplicity:
            raise ValueError(
                f"Edge label '{name}' has conflicting multiplicities: existing={existing.multiplicity}, requested={spec.multiplicity}"
            )
        self.edge_labels[name] = spec

    def ensure_vertex_index(
        self,
        name: str,
        key_specs: Sequence[GraphIndexKeySpec],
        unique: bool,
        kind: str = "composite",
        backend: Optional[str] = None,
        index_only: Optional[str] = None,
    ) -> None:
        self.vertex_indexes[name] = IndexSpec(
            name=name,
            key_specs=tuple(key_specs),
            unique=unique,
            kind=kind,
            backend=backend,
            index_only=index_only,
        )

    def ensure_edge_index(
        self,
        name: str,
        key_specs: Sequence[GraphIndexKeySpec],
        unique: bool,
        kind: str = "composite",
        backend: Optional[str] = None,
        index_only: Optional[str] = None,
    ) -> None:
        self.edge_indexes[name] = IndexSpec(
            name=name,
            key_specs=tuple(key_specs),
            unique=unique,
            kind=kind,
            backend=backend,
            index_only=index_only,
        )

    def ensure_vertex_property_constraint(self, label: str, property_keys: Iterable[str]) -> None:
        existing = self.vertex_property_constraints.get(label)
        merged = unique_preserve_order([*(existing.property_keys if existing else ()), *property_keys])
        self.vertex_property_constraints[label] = PropertyConstraintSpec(label=label, property_keys=tuple(merged))

    def ensure_edge_property_constraint(self, label: str, property_keys: Iterable[str]) -> None:
        existing = self.edge_property_constraints.get(label)
        merged = unique_preserve_order([*(existing.property_keys if existing else ()), *property_keys])
        self.edge_property_constraints[label] = PropertyConstraintSpec(label=label, property_keys=tuple(merged))

    def ensure_connection_constraint(self, edge_label: str, out_label: str, in_label: str) -> None:
        key = f"{edge_label}:{out_label}:{in_label}"
        self.connection_constraints[key] = ConnectionConstraintSpec(
            edge_label=edge_label,
            out_label=out_label,
            in_label=in_label,
        )

    def ensure_edge_relation_index(
        self,
        name: str,
        edge_label: str,
        direction: str,
        sort_order: str,
        property_keys: Sequence[str],
    ) -> None:
        self.edge_relation_indexes[name] = EdgeRelationIndexSpec(
            name=name,
            edge_label=edge_label,
            direction=direction,
            sort_order=sort_order,
            property_keys=tuple(property_keys),
        )

    def ensure_property_relation_index(
        self,
        name: str,
        property_key: str,
        sort_order: str,
        meta_property_keys: Sequence[str],
    ) -> None:
        self.property_relation_indexes[name] = PropertyRelationIndexSpec(
            name=name,
            property_key=property_key,
            sort_order=sort_order,
            meta_property_keys=tuple(meta_property_keys),
        )

    def as_binding(self) -> Dict[str, Any]:
        return {
            "property_keys": [asdict(item) for item in self.property_keys.values()],
            "vertex_labels": [asdict(item) for item in self.vertex_labels.values()],
            "edge_labels": [asdict(item) for item in self.edge_labels.values()],
            "vertex_indexes": [item.to_dict() for item in self.vertex_indexes.values()],
            "edge_indexes": [item.to_dict() for item in self.edge_indexes.values()],
            "vertex_property_constraints": [item.to_dict() for item in self.vertex_property_constraints.values()],
            "edge_property_constraints": [item.to_dict() for item in self.edge_property_constraints.values()],
            "connection_constraints": [item.to_dict() for item in self.connection_constraints.values()],
            "edge_relation_indexes": [item.to_dict() for item in self.edge_relation_indexes.values()],
            "property_relation_indexes": [item.to_dict() for item in self.property_relation_indexes.values()],
        }

    def to_dict(self) -> Dict[str, Any]:
        return self.as_binding()


class SchemaPlanner:
    def __init__(self, config: LoaderConfig, ontology: OntologyCatalog) -> None:
        self.config = config
        self.ontology = ontology
        self._name_registry: Dict[Tuple[str, str], str] = {}
        self._class_labels_by_iri = {item.iri: item.resolved_vertex_label() for item in config.classes}

    def build(self) -> SchemaPlan:
        plan = SchemaPlan()

        system_vertex_property_keys = [spec["name"] for spec in SYSTEM_VERTEX_PROPERTY_SPECS]
        system_edge_property_keys = [spec["name"] for spec in SYSTEM_EDGE_PROPERTY_SPECS]

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
            label = class_mapping.resolved_vertex_label()
            mapped_class_iris.add(class_mapping.iri)
            self._register_name("vertex_label", label, class_mapping.iri)
            plan.ensure_vertex_label(label, class_mapping.iri)

            bound_property_keys = list(system_vertex_property_keys)
            for prop in class_mapping.properties:
                property_key = prop.resolved_property_key()
                mapped_property_iris.add(prop.iri)
                self._register_name("property", property_key, prop.iri)
                plan.ensure_property_key(property_key, self.resolve_property_data_type(prop), prop.cardinality)
                bound_property_keys.append(property_key)

                for meta_prop in prop.meta_properties:
                    self._register_name(
                        "property",
                        meta_prop.property_key,
                        f"meta:{meta_prop.property_key}",
                    )
                    plan.ensure_property_key(meta_prop.property_key, meta_prop.data_type, "SINGLE")

            plan.ensure_vertex_property_constraint(label, bound_property_keys)

        for relationship in self.config.relationships:
            edge_label = relationship.resolved_edge_label()
            mapped_relationship_iris.add(relationship.iri)
            self._register_name("edge_label", edge_label, relationship.iri)
            plan.ensure_edge_label(edge_label, relationship.resolved_multiplicity(self.ontology), relationship.iri)

            bound_property_keys = list(system_edge_property_keys)
            for prop in relationship.properties:
                property_key = prop.resolved_property_key()
                mapped_property_iris.add(prop.iri)
                self._register_name("property", property_key, prop.iri)
                plan.ensure_property_key(property_key, self.resolve_property_data_type(prop), prop.cardinality)
                bound_property_keys.append(property_key)

            plan.ensure_edge_property_constraint(edge_label, bound_property_keys)
            plan.ensure_connection_constraint(
                edge_label=edge_label,
                out_label=self._class_labels_by_iri[relationship.source_class_iri],
                in_label=self._class_labels_by_iri[relationship.target_class_iri],
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
                        plan.ensure_edge_label(label, "MULTI", ontology_property.iri)
                else:
                    if ontology_property.iri not in mapped_property_iris:
                        key = safe_name(local_name(ontology_property.iri))
                        self._register_name("property", key, ontology_property.iri)
                        plan.ensure_property_key(key, infer_data_type_from_ranges(ontology_property.ranges), "SINGLE")

        plan.ensure_vertex_index(
            "byExternalId",
            [GraphIndexKeySpec(property_key="external_id")],
            True,
        )
        plan.ensure_edge_index(
            "byEdgeExternalId",
            [GraphIndexKeySpec(property_key="edge_external_id")],
            False,
        )

        if not self.config.indexes:
            self._add_auto_graph_indexes(plan)

        for index in self.config.indexes:
            self._register_name("graph_index", index.name, index.name)
            key_specs = self._graph_index_key_specs(index)
            if index.element == "vertex":
                plan.ensure_vertex_index(
                    name=index.name,
                    key_specs=key_specs,
                    unique=index.unique,
                    kind=index.kind,
                    backend=index.backend,
                    index_only=index.index_only,
                )
            else:
                plan.ensure_edge_index(
                    name=index.name,
                    key_specs=key_specs,
                    unique=index.unique,
                    kind=index.kind,
                    backend=index.backend,
                    index_only=index.index_only,
                )

        if not self.config.edge_relation_indexes:
            self._add_auto_edge_relation_indexes(plan)

        for index in self.config.edge_relation_indexes:
            self._register_name(
                "edge_relation_index",
                index.name,
                f"edge_relation_index:{index.edge_label}:{index.name}",
            )
            plan.ensure_edge_relation_index(
                name=index.name,
                edge_label=index.edge_label,
                direction=index.direction,
                sort_order=index.sort_order,
                property_keys=index.property_keys,
            )

        if not self.config.property_relation_indexes:
            self._add_auto_property_relation_indexes(plan)

        for index in self.config.property_relation_indexes:
            self._register_name(
                "property_relation_index",
                index.name,
                f"property_relation_index:{index.property_key}:{index.name}",
            )
            plan.ensure_property_relation_index(
                name=index.name,
                property_key=index.property_key,
                sort_order=index.sort_order,
                meta_property_keys=index.meta_property_keys,
            )

        return plan

    def resolve_property_data_type(self, prop: PropertyMapping) -> str:
        if prop.data_type:
            return normalize_data_type(prop.data_type)
        ontology_property = self.ontology.properties.get(prop.iri)
        if ontology_property:
            return infer_data_type_from_ranges(ontology_property.ranges)
        return "String"

    def _graph_index_key_specs(self, index: GraphIndexDefinition) -> Sequence[GraphIndexKeySpec]:
        key_specs: list[GraphIndexKeySpec] = []
        for item in index.ordered_key_options():
            key_specs.append(
                GraphIndexKeySpec(
                    property_key=item.property_key,
                    mapping=item.mapping,
                    mapped_name=item.mapped_name,
                    text_analyzer=item.text_analyzer,
                    string_analyzer=item.string_analyzer,
                    custom_parameters=dict(item.custom_parameters),
                    geo_max_levels=item.geo_max_levels,
                    geo_dist_error_pct=item.geo_dist_error_pct,
                )
            )
        return key_specs

    def _add_auto_graph_indexes(self, plan: SchemaPlan) -> None:
        for class_mapping in self.config.classes:
            label = class_mapping.resolved_vertex_label()
            for prop in class_mapping.properties:
                if not supports_auto_graph_index(prop.cardinality):
                    continue
                property_key = prop.resolved_property_key()
                name = safe_name(f"auto_vertex_{label}_{property_key}")
                self._register_name("graph_index", name, f"auto:vertex:{label}:{property_key}")
                plan.ensure_vertex_index(
                    name=name,
                    key_specs=[GraphIndexKeySpec(property_key=property_key)],
                    unique=False,
                    kind="composite",
                    index_only=label,
                )

        for relationship in self.config.relationships:
            edge_label = relationship.resolved_edge_label()
            for prop in relationship.properties:
                if not supports_auto_graph_index(prop.cardinality):
                    continue
                property_key = prop.resolved_property_key()
                name = safe_name(f"auto_edge_{edge_label}_{property_key}")
                self._register_name("graph_index", name, f"auto:edge:{edge_label}:{property_key}")
                plan.ensure_edge_index(
                    name=name,
                    key_specs=[GraphIndexKeySpec(property_key=property_key)],
                    unique=False,
                    kind="composite",
                    index_only=edge_label,
                )

    def _add_auto_edge_relation_indexes(self, plan: SchemaPlan) -> None:
        for relationship in self.config.relationships:
            edge_label = relationship.resolved_edge_label()
            for prop in relationship.properties:
                property_key = prop.resolved_property_key()
                data_type = self.resolve_property_data_type(prop)
                if not supports_auto_graph_index(prop.cardinality) or not supports_relation_index_data_type(data_type):
                    continue
                name = safe_name(f"auto_edge_rel_{edge_label}_{property_key}")
                self._register_name(
                    "edge_relation_index",
                    name,
                    f"auto:edge_relation:{edge_label}:{property_key}",
                )
                plan.ensure_edge_relation_index(
                    name=name,
                    edge_label=edge_label,
                    direction="BOTH",
                    sort_order=default_relation_index_sort_order(data_type),
                    property_keys=[property_key],
                )

    def _add_auto_property_relation_indexes(self, plan: SchemaPlan) -> None:
        for class_mapping in self.config.classes:
            for prop in class_mapping.properties:
                if normalize_cardinality(prop.cardinality) == "SINGLE":
                    continue
                property_key = prop.resolved_property_key()
                for meta_prop in prop.meta_properties:
                    if not supports_relation_index_data_type(meta_prop.data_type):
                        continue
                    name = safe_name(f"auto_property_rel_{property_key}_{meta_prop.property_key}")
                    self._register_name(
                        "property_relation_index",
                        name,
                        f"auto:property_relation:{property_key}:{meta_prop.property_key}",
                    )
                    plan.ensure_property_relation_index(
                        name=name,
                        property_key=property_key,
                        sort_order=default_relation_index_sort_order(meta_prop.data_type),
                        meta_property_keys=[meta_prop.property_key],
                    )

    def _register_name(self, namespace: str, name: str, iri: str) -> None:
        key = (namespace, name)
        existing = self._name_registry.get(key)
        if existing and existing != iri:
            raise ValueError(
                f"Name collision for {namespace} '{name}': both '{existing}' and '{iri}' resolve to the same JanusGraph name"
            )
        self._name_registry[key] = iri
