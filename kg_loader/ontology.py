from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import LoaderConfig
from .constants import XSD_TO_JANUSGRAPH
from .utils import (
    coerce_value_for_transport,
    infer_data_type_from_ranges,
    multiplicity_supports_incoming_functional,
    multiplicity_supports_outgoing_functional,
    normalize_cardinality,
    normalize_data_type,
)

LOG = logging.getLogger("kg_loader")


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


def load_ontology(definition: Any) -> OntologyCatalog:
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

    def restriction_literal_value(literal: Any) -> Tuple[Any, Optional[str]]:
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
