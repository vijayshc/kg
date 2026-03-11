from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from .config import LoaderConfig, QueryPatternDefinition
from .schema import SchemaPlan
from .utils import (
    default_relation_index_sort_order,
    normalize_cardinality,
    safe_name,
    supports_auto_graph_index,
    supports_relation_index_data_type,
)

TEXT_OPERATORS = {
    "textcontains",
    "textcontainsprefix",
    "textcontainsregex",
    "textcontainsfuzzy",
    "textcontainsphrase",
    "textprefix",
    "textregex",
    "textfuzzy",
}
RANGE_OPERATORS = {"gt", "gte", "lt", "lte", "inside", "outside", "range"}
EXACT_OPERATORS = {"eq", "neq"}


@dataclass
class IndexRecommendation:
    name: str
    category: str
    status: str
    rationale: str
    details: Dict[str, Any]
    covered_by: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "name": self.name,
            "category": self.category,
            "status": self.status,
            "rationale": self.rationale,
            "details": self.details,
        }
        if self.covered_by:
            payload["covered_by"] = self.covered_by
        return payload


def recommend_query_pattern_indexes(config: LoaderConfig, schema_plan: SchemaPlan) -> List[IndexRecommendation]:
    if not config.query_patterns:
        return _derive_default_recommendations(config, schema_plan)

    recommendations: List[IndexRecommendation] = []
    for pattern in config.query_patterns:
        if pattern.pattern_type == "graph":
            recommendations.append(_recommend_graph_index(pattern, schema_plan))
        elif pattern.pattern_type == "traversal":
            recommendations.append(_recommend_edge_relation_index(pattern, schema_plan))
        else:
            recommendations.append(_recommend_property_relation_index(pattern, schema_plan))
    return recommendations


def _recommend_graph_index(pattern: QueryPatternDefinition, schema_plan: SchemaPlan) -> IndexRecommendation:
    operators = [item.operator for item in pattern.predicates]
    property_keys = [item.property_key for item in pattern.predicates]

    uses_mixed = any(item in TEXT_OPERATORS or item in RANGE_OPERATORS for item in operators)
    if pattern.order_by and pattern.order_by not in property_keys:
        property_keys.append(pattern.order_by)
        uses_mixed = True

    key_specs: List[Dict[str, Any]] = []
    for predicate in pattern.predicates:
        spec: Dict[str, Any] = {"property_key": predicate.property_key}
        if predicate.operator in TEXT_OPERATORS:
            spec["mapping"] = "TEXT"
        key_specs.append(spec)
    if pattern.order_by and pattern.order_by not in {item["property_key"] for item in key_specs}:
        key_specs.append({"property_key": pattern.order_by})

    details: Dict[str, Any] = {
        "pattern_type": pattern.pattern_type,
        "element": pattern.element,
        "label": pattern.label,
        "kind": "mixed" if uses_mixed else "composite",
        "property_keys": property_keys,
        "keys": key_specs,
        "index_only": pattern.label,
    }
    if uses_mixed:
        details["backend"] = "search"

    covered_by = _match_graph_index(details, schema_plan)
    status = "satisfied" if covered_by else "recommended"
    rationale = (
        "Graph lookup uses text/range/order predicates that benefit from a mixed index"
        if uses_mixed
        else "Graph lookup uses equality predicates that are a good fit for a composite graph index"
    )
    return IndexRecommendation(
        name=safe_name(pattern.name),
        category="graph_index",
        status=status,
        rationale=rationale,
        details=details,
        covered_by=covered_by,
    )


def _derive_default_recommendations(config: LoaderConfig, schema_plan: SchemaPlan) -> List[IndexRecommendation]:
    recommendations: List[IndexRecommendation] = []

    for class_mapping in config.classes:
        label = class_mapping.resolved_vertex_label()
        for prop in class_mapping.properties:
            property_key = prop.resolved_property_key()
            if supports_auto_graph_index(prop.cardinality):
                details: Dict[str, Any] = {
                    "pattern_type": "graph",
                    "element": "vertex",
                    "label": label,
                    "kind": "composite",
                    "property_keys": [property_key],
                    "keys": [{"property_key": property_key}],
                    "index_only": label,
                }
                covered_by = _match_graph_index(details, schema_plan)
                recommendations.append(
                    IndexRecommendation(
                        name=safe_name(f"{label} {property_key} Exact Lookup"),
                        category="graph_index",
                        status="satisfied" if covered_by else "recommended",
                        rationale="Exact-match vertex lookups are best served by label-scoped composite graph indexes.",
                        details=details,
                        covered_by=covered_by,
                    )
                )

            if normalize_cardinality(prop.cardinality) != "SINGLE":
                for meta_prop in prop.meta_properties:
                    if not supports_relation_index_data_type(meta_prop.data_type):
                        continue
                    details = {
                        "pattern_type": "property_meta",
                        "property_key": property_key,
                        "sort_order": default_relation_index_sort_order(meta_prop.data_type),
                        "meta_property_keys": [meta_prop.property_key],
                    }
                    covered_by = _match_property_relation_index(details, schema_plan)
                    recommendations.append(
                        IndexRecommendation(
                            name=safe_name(f"{property_key} {meta_prop.property_key} Meta Traversal"),
                            category="property_relation_index",
                            status="satisfied" if covered_by else "recommended",
                            rationale="Repeated vertex-property values with meta-properties benefit from property relation indexes.",
                            details=details,
                            covered_by=covered_by,
                        )
                    )

    for relationship in config.relationships:
        edge_label = relationship.resolved_edge_label()
        start_label = next(
            item.resolved_vertex_label()
            for item in config.classes
            if item.iri == relationship.source_class_iri
        )
        for prop in relationship.properties:
            property_key = prop.resolved_property_key()
            data_type = schema_plan.property_keys[property_key].data_type

            if supports_auto_graph_index(prop.cardinality):
                details = {
                    "pattern_type": "graph",
                    "element": "edge",
                    "label": edge_label,
                    "kind": "composite",
                    "property_keys": [property_key],
                    "keys": [{"property_key": property_key}],
                    "index_only": edge_label,
                }
                covered_by = _match_graph_index(details, schema_plan)
                recommendations.append(
                    IndexRecommendation(
                        name=safe_name(f"{edge_label} {property_key} Edge Lookup"),
                        category="graph_index",
                        status="satisfied" if covered_by else "recommended",
                        rationale="Exact-match edge lookups are best served by label-scoped composite graph indexes.",
                        details=details,
                        covered_by=covered_by,
                    )
                )

            if supports_auto_graph_index(prop.cardinality) and supports_relation_index_data_type(data_type):
                details = {
                    "pattern_type": "traversal",
                    "edge_label": edge_label,
                    "start_label": start_label,
                    "direction": "BOTH",
                    "sort_order": default_relation_index_sort_order(data_type),
                    "property_keys": [property_key],
                }
                covered_by = _match_edge_relation_index(details, schema_plan)
                recommendations.append(
                    IndexRecommendation(
                        name=safe_name(f"{edge_label} {property_key} Incident Traversal"),
                        category="edge_relation_index",
                        status="satisfied" if covered_by else "recommended",
                        rationale="Incident-edge traversals with edge-property filters benefit from vertex-centric edge relation indexes.",
                        details=details,
                        covered_by=covered_by,
                    )
                )

    return recommendations


def _recommend_edge_relation_index(pattern: QueryPatternDefinition, schema_plan: SchemaPlan) -> IndexRecommendation:
    equality_keys = [item.property_key for item in pattern.predicates if item.operator in EXACT_OPERATORS]
    range_or_text_keys = [item.property_key for item in pattern.predicates if item.operator not in EXACT_OPERATORS]
    ordered_keys = [*equality_keys, *[key for key in range_or_text_keys if key not in equality_keys]]
    if pattern.order_by and pattern.order_by not in ordered_keys:
        ordered_keys.append(pattern.order_by)

    details = {
        "pattern_type": pattern.pattern_type,
        "edge_label": pattern.edge_label,
        "start_label": pattern.start_label,
        "direction": pattern.direction,
        "sort_order": pattern.order,
        "property_keys": ordered_keys,
    }
    covered_by = _match_edge_relation_index(details, schema_plan)
    status = "satisfied" if covered_by else "recommended"
    rationale = (
        "Traversal filters or orders incident edges by edge properties; a vertex-centric edge index reduces in-memory filtering "
        "for high-degree vertices"
    )
    return IndexRecommendation(
        name=safe_name(pattern.name),
        category="edge_relation_index",
        status=status,
        rationale=rationale,
        details=details,
        covered_by=covered_by,
    )


def _recommend_property_relation_index(pattern: QueryPatternDefinition, schema_plan: SchemaPlan) -> IndexRecommendation:
    meta_property_keys = [item.property_key for item in pattern.predicates]
    if pattern.order_by and pattern.order_by not in meta_property_keys:
        meta_property_keys.append(pattern.order_by)

    details = {
        "pattern_type": pattern.pattern_type,
        "property_key": pattern.property_key,
        "sort_order": pattern.order,
        "meta_property_keys": meta_property_keys,
    }
    covered_by = _match_property_relation_index(details, schema_plan)
    status = "satisfied" if covered_by else "recommended"
    rationale = (
        "Query traverses vertex properties by meta-properties; a JanusGraph property relation index speeds up meta-property lookups"
    )
    return IndexRecommendation(
        name=safe_name(pattern.name),
        category="property_relation_index",
        status=status,
        rationale=rationale,
        details=details,
        covered_by=covered_by,
    )


def _match_graph_index(candidate: Dict[str, Any], schema_plan: SchemaPlan) -> Optional[str]:
    candidate_keys = tuple(candidate.get("property_keys", []))
    expected_kind = candidate.get("kind")
    expected_scope = candidate.get("index_only")
    expected_element = candidate.get("element")
    index_pool = schema_plan.vertex_indexes if expected_element == "vertex" else schema_plan.edge_indexes
    for name, spec in index_pool.items():
        if spec.kind != expected_kind:
            continue
        if spec.index_only != expected_scope:
            continue
        if spec.property_keys == candidate_keys:
            return name
    return None


def _match_edge_relation_index(candidate: Dict[str, Any], schema_plan: SchemaPlan) -> Optional[str]:
    candidate_keys = tuple(candidate.get("property_keys", []))
    for name, spec in schema_plan.edge_relation_indexes.items():
        if spec.edge_label != candidate.get("edge_label"):
            continue
        if spec.direction != candidate.get("direction"):
            continue
        if spec.sort_order != candidate.get("sort_order"):
            continue
        if spec.property_keys == candidate_keys:
            return name
    return None


def _match_property_relation_index(candidate: Dict[str, Any], schema_plan: SchemaPlan) -> Optional[str]:
    candidate_keys = tuple(candidate.get("meta_property_keys", []))
    for name, spec in schema_plan.property_relation_indexes.items():
        if spec.property_key != candidate.get("property_key"):
            continue
        if spec.sort_order != candidate.get("sort_order"):
            continue
        if spec.meta_property_keys == candidate_keys:
            return name
    return None
