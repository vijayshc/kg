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
import org.apache.tinkerpop.gremlin.structure.Edge
import org.apache.tinkerpop.gremlin.structure.Vertex
import org.janusgraph.core.Cardinality
import org.janusgraph.core.Multiplicity

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

  schema.vertex_indexes.each { spec ->
    if (mgmt.getGraphIndex(spec.name) == null) {
      def key = mgmt.getPropertyKey(spec.property_key)
      def builder = mgmt.buildIndex(spec.name, Vertex.class).addKey(key)
      if (spec.unique) {
        builder.unique()
      }
      builder.buildCompositeIndex()
    }
  }

  schema.edge_indexes.each { spec ->
    if (mgmt.getGraphIndex(spec.name) == null) {
      def key = mgmt.getPropertyKey(spec.property_key)
      def builder = mgmt.buildIndex(spec.name, Edge.class).addKey(key)
      if (spec.unique) {
        builder.unique()
      }
      builder.buildCompositeIndex()
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
return 'schema-ok'
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

def processed = 0
rows.each { row ->
    def vertex = g.V().hasLabel(vertexLabel).has(idKey, row.external_id).fold()
            .coalesce(unfold(), addV(vertexLabel).property(VertexProperty.Cardinality.single, idKey, row.external_id))
      .next()

  row.properties.each { prop ->
    if (prop.value != null) {
      vertex.property(cardinality(prop.cardinality), prop.key, coerce(prop.value, prop.data_type))
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
      vertex.property(cardinality(prop.cardinality), prop.key, coerce(prop.value, prop.data_type))
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

    def ensure_schema(self, schema_plan: SchemaPlan) -> None:
        LOG.info(
            "Ensuring JanusGraph schema: %s vertex labels, %s edge labels, %s property keys",
            len(schema_plan.vertex_labels),
            len(schema_plan.edge_labels),
            len(schema_plan.property_keys),
        )
        self.submit(
            build_management_script(self.settings.graph_alias),
            bindings={"schema": schema_plan.as_binding()},
            include_graph=True,
        )

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
