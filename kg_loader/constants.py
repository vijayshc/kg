from __future__ import annotations

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
