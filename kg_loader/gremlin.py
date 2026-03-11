from __future__ import annotations

import logging
import os
import ssl
from typing import Any, Dict, List, Optional

from .config import JanusGraphSettings
from .data_clients import deduplicate_payload_rows
from .schema import SchemaPlan
from .utils import as_bool, validate_gremlin_identifier

LOG = logging.getLogger("kg_loader")

MANAGEMENT_SCRIPT_TEMPLATE = r'''
import org.apache.tinkerpop.gremlin.process.traversal.Order
import org.apache.tinkerpop.gremlin.structure.Direction
import org.apache.tinkerpop.gremlin.structure.Edge
import org.apache.tinkerpop.gremlin.structure.Vertex
import org.janusgraph.core.Cardinality
import org.janusgraph.core.Connection
import org.janusgraph.core.Multiplicity
import org.janusgraph.core.PropertyKey
import org.janusgraph.core.schema.Mapping
import org.janusgraph.core.schema.Parameter
import org.janusgraph.graphdb.types.ParameterType

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

def resolveDirection = { value ->
  switch ((value ?: 'BOTH').toUpperCase()) {
    case 'IN': return Direction.IN
    case 'OUT': return Direction.OUT
    default: return Direction.BOTH
  }
}

def resolveOrder = { value ->
  if (value == null) {
    return null
  }
  switch ((value ?: 'asc').toLowerCase()) {
    case 'desc': return Order.desc
    default: return Order.asc
  }
}

def buildIndexKeyParameters = { keySpec ->
  def params = []
  if (keySpec.mapping != null) {
    params << Mapping.valueOf(keySpec.mapping.toString().toUpperCase()).asParameter()
  }
  if (keySpec.mapped_name != null) {
    params << Parameter.of(ParameterType.MAPPED_NAME.getName(), keySpec.mapped_name.toString())
  }
  if (keySpec.string_analyzer != null) {
    params << Parameter.of(ParameterType.STRING_ANALYZER.getName(), keySpec.string_analyzer.toString())
  }
  if (keySpec.text_analyzer != null) {
    params << Parameter.of(ParameterType.TEXT_ANALYZER.getName(), keySpec.text_analyzer.toString())
  }
  if (keySpec.geo_max_levels != null) {
    params << Parameter.of(ParameterType.INDEX_GEO_MAX_LEVELS.getName(), keySpec.geo_max_levels)
  }
  if (keySpec.geo_dist_error_pct != null) {
    params << Parameter.of(ParameterType.INDEX_GEO_DIST_ERROR_PCT.getName(), keySpec.geo_dist_error_pct)
  }
  (keySpec.custom_parameters ?: [:]).each { parameterName, parameterValue ->
    params << Parameter.of(ParameterType.customParameterName(parameterName.toString()), parameterValue)
  }
  return params as Object[]
}

def buildGraphIndex = { spec, elementClass, labelResolver ->
  def builder = mgmt.buildIndex(spec.name, elementClass)
  (spec.keys ?: []).each { keySpec ->
    def key = mgmt.getPropertyKey(keySpec.property_key)
    def params = buildIndexKeyParameters(keySpec)
    if (params.length > 0) {
      builder.addKey(key, *params)
    } else {
      builder.addKey(key)
    }
  }
  if (spec.index_only != null) {
    builder.indexOnly(labelResolver(spec.index_only))
  }
  if (((spec.kind ?: 'composite').toString()).equalsIgnoreCase('mixed')) {
    builder.buildMixedIndex(spec.backend.toString())
  } else {
    if (spec.unique) {
      builder.unique()
    }
    builder.buildCompositeIndex()
  }
}

def createdGraphIndexes = []
def createdRelationIndexes = []
def appliedVertexPropertyConstraints = []
def appliedEdgePropertyConstraints = []
def appliedConnectionConstraints = []
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

  if (createSchemaConstraints) {
    schema.vertex_property_constraints.each { spec ->
      def label = mgmt.getVertexLabel(spec.label)
      def existing = ((label?.mappedProperties() ?: []) as Collection).collect { it.name() } as Set
      def missing = spec.property_keys.findAll { !existing.contains(it) }.collect { mgmt.getPropertyKey(it) }
      if (!missing.isEmpty()) {
        mgmt.addProperties(label, *(missing as PropertyKey[]))
        appliedVertexPropertyConstraints << [label: spec.label, property_keys: missing.collect { it.name() }]
      }
    }

    schema.edge_property_constraints.each { spec ->
      def edgeLabel = mgmt.getEdgeLabel(spec.label)
      def existing = ((edgeLabel?.mappedProperties() ?: []) as Collection).collect { it.name() } as Set
      def missing = spec.property_keys.findAll { !existing.contains(it) }.collect { mgmt.getPropertyKey(it) }
      if (!missing.isEmpty()) {
        mgmt.addProperties(edgeLabel, *(missing as PropertyKey[]))
        appliedEdgePropertyConstraints << [label: spec.label, property_keys: missing.collect { it.name() }]
      }
    }

    schema.connection_constraints.each { spec ->
      def edgeLabel = mgmt.getEdgeLabel(spec.edge_label)
      def outLabel = mgmt.getVertexLabel(spec.out_label)
      def inLabel = mgmt.getVertexLabel(spec.in_label)
      def exists = ((edgeLabel?.mappedConnections() ?: []) as Collection).any { Connection connection ->
        connection.getOutgoingVertexLabel().name() == spec.out_label && connection.getIncomingVertexLabel().name() == spec.in_label
      }
      if (!exists) {
        mgmt.addConnection(edgeLabel, outLabel, inLabel)
        appliedConnectionConstraints << [edge_label: spec.edge_label, out_label: spec.out_label, in_label: spec.in_label]
      }
    }
  }

  schema.vertex_indexes.each { spec ->
    if (mgmt.getGraphIndex(spec.name) == null) {
      buildGraphIndex(spec, Vertex.class, { labelName -> mgmt.getVertexLabel(labelName) })
      createdGraphIndexes << spec.name
    }
  }

  schema.edge_indexes.each { spec ->
    if (mgmt.getGraphIndex(spec.name) == null) {
      buildGraphIndex(spec, Edge.class, { labelName -> mgmt.getEdgeLabel(labelName) })
      createdGraphIndexes << spec.name
    }
  }

  schema.edge_relation_indexes.each { spec ->
    def edgeLabel = mgmt.getEdgeLabel(spec.edge_label)
    if (!mgmt.containsRelationIndex(edgeLabel, spec.name)) {
      def propertyKeys = spec.property_keys.collect { mgmt.getPropertyKey(it) } as PropertyKey[]
      def order = resolveOrder(spec.sort_order)
      if (order != null) {
        mgmt.buildEdgeIndex(edgeLabel, spec.name, resolveDirection(spec.direction), order, *propertyKeys)
      } else {
        mgmt.buildEdgeIndex(edgeLabel, spec.name, resolveDirection(spec.direction), *propertyKeys)
      }
      createdRelationIndexes << [name: spec.name, relation_type: spec.edge_label, relation_kind: 'edge']
    }
  }

  schema.property_relation_indexes.each { spec ->
    def propertyKey = mgmt.getPropertyKey(spec.property_key)
    if (!mgmt.containsRelationIndex(propertyKey, spec.name)) {
      def metaPropertyKeys = spec.meta_property_keys.collect { mgmt.getPropertyKey(it) } as PropertyKey[]
      def order = resolveOrder(spec.sort_order)
      if (order != null) {
        mgmt.buildPropertyIndex(propertyKey, spec.name, order, *metaPropertyKeys)
      } else {
        mgmt.buildPropertyIndex(propertyKey, spec.name, *metaPropertyKeys)
      }
      createdRelationIndexes << [name: spec.name, relation_type: spec.property_key, relation_kind: 'property']
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
return [
  created_graph_indexes: createdGraphIndexes,
  created_relation_indexes: createdRelationIndexes,
  applied_vertex_property_constraints: appliedVertexPropertyConstraints,
  applied_edge_property_constraints: appliedEdgePropertyConstraints,
  applied_connection_constraints: appliedConnectionConstraints,
]
'''

AWAIT_GRAPH_INDEX_STATUS_SCRIPT = r'''
import org.janusgraph.core.schema.SchemaStatus
import org.janusgraph.graphdb.database.management.ManagementSystem

def desiredStatuses = (statuses ?: []).collect { SchemaStatus.valueOf(it.toString()) }
def reports = []
indexNames.each { indexName ->
  def waiter = ManagementSystem.awaitGraphIndexStatus(__GRAPH_ALIAS__, indexName)
  def report = desiredStatuses ? waiter.status(*desiredStatuses).call() : waiter.call()
  reports << String.valueOf(report)
}
return reports
'''

AWAIT_RELATION_INDEX_STATUS_SCRIPT = r'''
import org.janusgraph.core.schema.SchemaStatus
import org.janusgraph.graphdb.database.management.ManagementSystem

def desiredStatuses = (statuses ?: []).collect { SchemaStatus.valueOf(it.toString()) }
def reports = []
relationIndexes.each { spec ->
  def waiter = ManagementSystem.awaitRelationIndexStatus(__GRAPH_ALIAS__, spec.name.toString(), spec.relation_type.toString())
  def report = desiredStatuses ? waiter.status(*desiredStatuses).call() : waiter.call()
  reports << String.valueOf(report)
}
return reports
'''

UPDATE_GRAPH_INDEX_SCRIPT = r'''
import org.janusgraph.core.schema.SchemaAction

def actionEnum = SchemaAction.valueOf(action.toString())
mgmt = __GRAPH_ALIAS__.openManagement()
try {
  indexNames.each { indexName ->
    def index = mgmt.getGraphIndex(indexName)
    if (index == null) {
      return
    }
    if (actionEnum == SchemaAction.REINDEX && concurrency != null && concurrency > 0) {
      mgmt.updateIndex(index, actionEnum, concurrency).get()
    } else {
      mgmt.updateIndex(index, actionEnum).get()
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
return indexNames
'''

UPDATE_RELATION_INDEX_SCRIPT = r'''
import org.janusgraph.core.schema.SchemaAction

def actionEnum = SchemaAction.valueOf(action.toString())
mgmt = __GRAPH_ALIAS__.openManagement()
try {
  relationIndexes.each { spec ->
    def relationType = mgmt.getRelationType(spec.relation_type.toString())
    def index = relationType == null ? null : mgmt.getRelationIndex(relationType, spec.name.toString())
    if (index == null) {
      return
    }
    if (actionEnum == SchemaAction.REINDEX && concurrency != null && concurrency > 0) {
      mgmt.updateIndex(index, actionEnum, concurrency).get()
    } else {
      mgmt.updateIndex(index, actionEnum).get()
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
return relationIndexes.collect { it.name }
'''

LIST_GRAPH_INDEXES_SCRIPT = r'''
mgmt = __GRAPH_ALIAS__.openManagement()
try {
  return indexNames.collect { indexName ->
    def index = mgmt.getGraphIndex(indexName)
    if (index == null) {
      return null
    }
    def statuses = [:]
    index.getFieldKeys().each { key ->
      statuses[key.name()] = String.valueOf(index.getIndexStatus(key))
    }
    def payload = [
      name: index.name(),
      property_keys: index.getFieldKeys().collect { it.name() },
      statuses: statuses,
      kind: index.isMixedIndex() ? 'mixed' : 'composite'
    ]
    if (index.isMixedIndex()) {
      payload.backend = index.getBackingIndex()
    }
    def indexOnly = mgmt.getIndexOnlyConstraint(index.name())
    if (indexOnly != null) {
      payload.index_only = indexOnly.name()
    }
    return payload
  }.findAll { it != null }
} finally {
  mgmt.rollback()
}
'''

LIST_RELATION_INDEXES_SCRIPT = r'''
mgmt = __GRAPH_ALIAS__.openManagement()
try {
  return relationIndexes.collect { spec ->
    def relationType = mgmt.getRelationType(spec.relation_type.toString())
    def index = relationType == null ? null : mgmt.getRelationIndex(relationType, spec.name.toString())
    if (index == null) {
      return null
    }
    return [
      name: index.name(),
      relation_type: spec.relation_type,
      relation_kind: spec.relation_kind,
      property_keys: index.getSortKey().collect { it.name() },
      direction: String.valueOf(index.getDirection()),
      sort_order: String.valueOf(index.getSortOrder()),
      status: String.valueOf(index.getIndexStatus()),
    ]
  }.findAll { it != null }
} finally {
  mgmt.rollback()
}
'''

LIST_SCHEMA_CONSTRAINTS_SCRIPT = r'''
mgmt = __GRAPH_ALIAS__.openManagement()
try {
  return [
    vertex_property_constraints: vertexLabels.collect { labelName ->
      def label = mgmt.getVertexLabel(labelName)
      [label: labelName, property_keys: ((label?.mappedProperties() ?: []) as Collection).collect { it.name() }.sort()]
    },
    edge_property_constraints: edgeLabels.collect { labelName ->
      def edgeLabel = mgmt.getEdgeLabel(labelName)
      [label: labelName, property_keys: ((edgeLabel?.mappedProperties() ?: []) as Collection).collect { it.name() }.sort()]
    },
    connection_constraints: edgeLabels.collectMany { labelName ->
      def edgeLabel = mgmt.getEdgeLabel(labelName)
      ((edgeLabel?.mappedConnections() ?: []) as Collection).collect { connection ->
        [
          edge_label: labelName,
          out_label: connection.getOutgoingVertexLabel().name(),
          in_label: connection.getIncomingVertexLabel().name(),
        ]
      }
    },
  ]
} finally {
  mgmt.rollback()
}
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

def buildMetaKeyValues = { metaProperties ->
  def keyValues = []
  (metaProperties ?: []).each { meta ->
    if (meta.value != null) {
      keyValues << meta.key
      keyValues << coerce(meta.value, meta.data_type)
    }
  }
  return keyValues as Object[]
}

def processed = 0
rows.each { row ->
  def vertex = g.V().hasLabel(vertexLabel).has(idKey, row.external_id).fold()
      .coalesce(unfold(), addV(vertexLabel).property(VertexProperty.Cardinality.single, idKey, row.external_id))
      .next()

  row.properties.each { prop ->
    if (prop.value != null) {
      def metaKeyValues = buildMetaKeyValues(prop.meta_properties)
      if (metaKeyValues.length > 0) {
        vertex.property(cardinality(prop.cardinality), prop.key, coerce(prop.value, prop.data_type), *metaKeyValues)
      } else {
        vertex.property(cardinality(prop.cardinality), prop.key, coerce(prop.value, prop.data_type))
      }
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

def buildMetaKeyValues = { metaProperties ->
  def keyValues = []
  (metaProperties ?: []).each { meta ->
    if (meta.value != null) {
      keyValues << meta.key
      keyValues << coerce(meta.value, meta.data_type)
    }
  }
  return keyValues as Object[]
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
      def metaKeyValues = buildMetaKeyValues(prop.meta_properties)
      if (metaKeyValues.length > 0) {
        vertex.property(cardinality(prop.cardinality), prop.key, coerce(prop.value, prop.data_type), *metaKeyValues)
      } else {
        vertex.property(cardinality(prop.cardinality), prop.key, coerce(prop.value, prop.data_type))
      }
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


def build_management_script(graph_alias: str) -> str:
    validate_gremlin_identifier(graph_alias, "JanusGraph graph alias")
    return MANAGEMENT_SCRIPT_TEMPLATE.replace("__GRAPH_ALIAS__", graph_alias)


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

    def ensure_schema(self, schema_plan: SchemaPlan) -> Dict[str, Any]:
        LOG.info(
            "Ensuring JanusGraph schema: %s vertex labels, %s edge labels, %s property keys",
            len(schema_plan.vertex_labels),
            len(schema_plan.edge_labels),
            len(schema_plan.property_keys),
        )
        result = self.submit(
            build_management_script(self.settings.graph_alias),
            bindings={
                "schema": schema_plan.as_binding(),
                "createSchemaConstraints": self.settings.create_schema_constraints,
            },
            include_graph=True,
        )
        summary = dict(result[0]) if result else {}

        planned_graph_index_names = [
            *schema_plan.vertex_indexes.keys(),
            *schema_plan.edge_indexes.keys(),
        ]
        planned_relation_indexes = [
            *[
                {"name": item.name, "relation_type": item.edge_label, "relation_kind": "edge"}
                for item in schema_plan.edge_relation_indexes.values()
            ],
            *[
                {"name": item.name, "relation_type": item.property_key, "relation_kind": "property"}
                for item in schema_plan.property_relation_indexes.values()
            ],
        ]

        summary["index_activation_mode"] = self.settings.index_activation_mode
        summary["relation_index_activation_mode"] = self.settings.relation_index_activation_mode

        if planned_graph_index_names and self.settings.index_activation_mode != "skip":
            self.await_graph_indexes(planned_graph_index_names, ["REGISTERED", "ENABLED"])
            graph_index_details = self.list_graph_indexes(planned_graph_index_names)
            indexes_to_activate = [
                item["name"]
                for item in graph_index_details
                if any(status != "ENABLED" for status in item.get("statuses", {}).values())
            ]
            if indexes_to_activate:
                action = "REINDEX" if self.settings.index_activation_mode == "reindex" else "ENABLE_INDEX"
                self.update_graph_indexes(indexes_to_activate, action, self.settings.index_reindex_concurrency)
                self.await_graph_indexes(indexes_to_activate, ["ENABLED"])
            summary["activated_graph_indexes"] = indexes_to_activate
        else:
            summary["activated_graph_indexes"] = []
        summary["graph_indexes"] = self.list_graph_indexes(planned_graph_index_names)

        if planned_relation_indexes and self.settings.relation_index_activation_mode != "skip":
            self.await_relation_indexes(planned_relation_indexes, ["REGISTERED", "ENABLED"])
            relation_index_details = self.list_relation_indexes(planned_relation_indexes)
            relation_indexes_to_activate = [
                item
                for item in relation_index_details
                if item.get("status") != "ENABLED"
            ]
            if relation_indexes_to_activate:
                action = (
                    "REINDEX"
                    if self.settings.relation_index_activation_mode == "reindex"
                    else "ENABLE_INDEX"
                )
                self.update_relation_indexes(
                    relation_indexes_to_activate,
                    action,
                    self.settings.relation_index_reindex_concurrency,
                )
                self.await_relation_indexes(relation_indexes_to_activate, ["ENABLED"])
            summary["activated_relation_indexes"] = [item["name"] for item in relation_indexes_to_activate]
        else:
            summary["activated_relation_indexes"] = []
        summary["relation_indexes"] = self.list_relation_indexes(planned_relation_indexes)

        summary["schema_constraints"] = self.list_schema_constraints(
            list(schema_plan.vertex_labels.keys()),
            list(schema_plan.edge_labels.keys()),
        )
        return summary

    def await_graph_indexes(self, index_names: List[str], statuses: List[str]) -> List[Any]:
        if not index_names:
            return []
        return self.submit(
            AWAIT_GRAPH_INDEX_STATUS_SCRIPT.replace("__GRAPH_ALIAS__", self.settings.graph_alias),
            bindings={"indexNames": index_names, "statuses": statuses},
            include_graph=True,
        )

    def await_relation_indexes(self, relation_indexes: List[Dict[str, Any]], statuses: List[str]) -> List[Any]:
        if not relation_indexes:
            return []
        return self.submit(
            AWAIT_RELATION_INDEX_STATUS_SCRIPT.replace("__GRAPH_ALIAS__", self.settings.graph_alias),
            bindings={"relationIndexes": relation_indexes, "statuses": statuses},
            include_graph=True,
        )

    def update_graph_indexes(self, index_names: List[str], action: str, concurrency: int = 0) -> List[Any]:
        if not index_names:
            return []
        return self.submit(
            UPDATE_GRAPH_INDEX_SCRIPT.replace("__GRAPH_ALIAS__", self.settings.graph_alias),
            bindings={"indexNames": index_names, "action": action, "concurrency": concurrency},
            include_graph=True,
        )

    def update_relation_indexes(
        self,
        relation_indexes: List[Dict[str, Any]],
        action: str,
        concurrency: int = 0,
    ) -> List[Any]:
        if not relation_indexes:
            return []
        return self.submit(
            UPDATE_RELATION_INDEX_SCRIPT.replace("__GRAPH_ALIAS__", self.settings.graph_alias),
            bindings={"relationIndexes": relation_indexes, "action": action, "concurrency": concurrency},
            include_graph=True,
        )

    def list_graph_indexes(self, index_names: List[str]) -> List[Dict[str, Any]]:
        if not index_names:
            return []
        result = self.submit(
            LIST_GRAPH_INDEXES_SCRIPT.replace("__GRAPH_ALIAS__", self.settings.graph_alias),
            bindings={"indexNames": index_names},
            include_graph=True,
        )
        if len(result) == 1 and isinstance(result[0], list):
            result = result[0]
        return [dict(item) for item in result]

    def list_relation_indexes(self, relation_indexes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not relation_indexes:
            return []
        result = self.submit(
            LIST_RELATION_INDEXES_SCRIPT.replace("__GRAPH_ALIAS__", self.settings.graph_alias),
            bindings={"relationIndexes": relation_indexes},
            include_graph=True,
        )
        if len(result) == 1 and isinstance(result[0], list):
            result = result[0]
        return [dict(item) for item in result]

    def list_schema_constraints(self, vertex_labels: List[str], edge_labels: List[str]) -> Dict[str, Any]:
        result = self.submit(
            LIST_SCHEMA_CONSTRAINTS_SCRIPT.replace("__GRAPH_ALIAS__", self.settings.graph_alias),
            bindings={"vertexLabels": vertex_labels, "edgeLabels": edge_labels},
            include_graph=True,
        )
        summary = dict(result[0]) if result else {}
        summary.setdefault("vertex_property_constraints", [])
        summary.setdefault("edge_property_constraints", [])
        summary.setdefault("connection_constraints", [])
        return summary

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
