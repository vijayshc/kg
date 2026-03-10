#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys

from gremlin_python.driver.client import Client
from gremlin_python.driver.serializer import GraphSONSerializersV3d0


def main() -> int:
    url = os.getenv("JANUSGRAPH_URL", "ws://127.0.0.1:8182/gremlin")
    traversal_source = os.getenv("JANUSGRAPH_TRAVERSAL_SOURCE", "g")
    client = Client(url, traversal_source, message_serializer=GraphSONSerializersV3d0())
    try:
        vertex_counts = client.submit("g.V().groupCount().by(label())").all().result()[0]
        edge_counts = client.submit("g.E().groupCount().by(label())").all().result()[0]
        summary = {
            "janusgraph_url": url,
            "vertices_total": sum(vertex_counts.values()),
            "edges_total": sum(edge_counts.values()),
            "vertex_counts": dict(sorted(vertex_counts.items())),
            "edge_counts": dict(sorted(edge_counts.items())),
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
