from __future__ import annotations

import csv
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from .config import ClassMapping, PropertyMapping, RelationshipMapping
from .utils import normalize_row, unique_preserve_order, validate_sql_fragment, validate_sql_statement

LOG = logging.getLogger("kg_loader")


def build_select_sql(table: str, columns: Sequence[str], where_clause: Optional[str] = None) -> str:
    validate_sql_fragment(table, "table reference")
    select_columns = unique_preserve_order(list(columns))
    for column in select_columns:
        validate_sql_fragment(column, "column projection")
    sql = f"SELECT {', '.join(select_columns)} FROM {table}"
    if where_clause:
        validate_sql_fragment(where_clause, "where clause")
        sql += f" WHERE {where_clause}"
    return sql


def deduplicate_payload_rows(rows: List[Dict[str, Any]], identity_key: str) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for row in rows:
        row_id = row[identity_key]
        if row_id not in merged:
            copied = dict(row)
            copied["properties"] = list(row.get("properties", []))
            merged[row_id] = copied
            order.append(row_id)
            continue
        merged[row_id]["properties"].extend(row.get("properties", []))
    return [merged[item] for item in order]


def build_class_sql(class_mapping: ClassMapping, inline_properties: Sequence[PropertyMapping]) -> str:
    if class_mapping.source.sql:
        return class_mapping.source.sql

    if not class_mapping.source.table or not class_mapping.source.key_column:
        raise ValueError(f"Class '{class_mapping.iri}' needs source.table and source.key_column for generated SQL")

    columns = [class_mapping.source.key_column]
    columns.extend(prop.source_column for prop in inline_properties if prop.source_column)
    return build_select_sql(class_mapping.source.table, columns, class_mapping.source.where)


def build_property_sql(class_mapping: ClassMapping, prop: PropertyMapping) -> str:
    if prop.source_sql:
        return prop.source_sql

    source_table = prop.source_table or class_mapping.source.table
    entity_key_column = prop.entity_key_column or class_mapping.source.key_column
    class_key = class_mapping.source.key_column
    if not source_table or not entity_key_column or not class_key or not prop.source_column:
        raise ValueError(f"Property '{prop.iri}' does not have enough metadata to build SQL")

    entity_projection = entity_key_column
    if entity_key_column != class_key:
        entity_projection = f"{entity_key_column} AS {class_key}"
    columns = [entity_projection, prop.source_column]
    return build_select_sql(source_table, columns, prop.where)


def build_relationship_sql(
    relationship: RelationshipMapping,
    source_class: ClassMapping,
    target_class: ClassMapping,
) -> str:
    if relationship.source.sql:
        return relationship.source.sql

    if not relationship.source.table:
        raise ValueError(f"Relationship '{relationship.iri}' needs source.table or source.sql")
    if not relationship.source.from_column or not relationship.source.to_column:
        raise ValueError(f"Relationship '{relationship.iri}' needs source.from_column and source.to_column")
    if not source_class.source.key_column or not target_class.source.key_column:
        raise ValueError(f"Relationship '{relationship.iri}' requires source/target class key columns")

    columns: List[str] = []
    if relationship.source.from_column == source_class.source.key_column:
        columns.append(relationship.source.from_column)
    else:
        columns.append(f"{relationship.source.from_column} AS {source_class.source.key_column}")

    if relationship.source.to_column == target_class.source.key_column:
        columns.append(relationship.source.to_column)
    else:
        columns.append(f"{relationship.source.to_column} AS {target_class.source.key_column}")

    columns.extend(prop.source_column for prop in relationship.properties if prop.source_column)
    return build_select_sql(relationship.source.table, columns, relationship.source.where)


def _clean_csv_value(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped != "" else None


class TabularDataClient:
    def describe_class_source(self, class_mapping: ClassMapping, inline_properties: Sequence[PropertyMapping]) -> Dict[str, Any]:
        raise NotImplementedError

    def iter_class_batches(
        self,
        class_mapping: ClassMapping,
        inline_properties: Sequence[PropertyMapping],
        fetch_size: int,
    ) -> Iterator[List[Dict[str, Any]]]:
        raise NotImplementedError

    def describe_property_source(self, class_mapping: ClassMapping, prop: PropertyMapping) -> Dict[str, Any]:
        raise NotImplementedError

    def iter_property_batches(
        self,
        class_mapping: ClassMapping,
        prop: PropertyMapping,
        fetch_size: int,
    ) -> Iterator[List[Dict[str, Any]]]:
        raise NotImplementedError

    def describe_relationship_source(
        self,
        relationship: RelationshipMapping,
        source_class: ClassMapping,
        target_class: ClassMapping,
    ) -> Dict[str, Any]:
        raise NotImplementedError

    def iter_relationship_batches(
        self,
        relationship: RelationshipMapping,
        source_class: ClassMapping,
        target_class: ClassMapping,
        fetch_size: int,
    ) -> Iterator[List[Dict[str, Any]]]:
        raise NotImplementedError

    def close(self) -> None:
        return None


class TeradataClient(TabularDataClient):
    def __init__(self) -> None:
        self._connection: Any = None

    def connect(self) -> Any:
        if self._connection is not None:
            return self._connection

        try:
            import teradatasql
        except ImportError as exc:
            raise RuntimeError("teradatasql is required to read data from Teradata") from exc

        host = os.getenv("TERADATA_HOST", "")
        if not host:
            raise RuntimeError("TERADATA_HOST must be set")

        connect_args: Dict[str, Any] = {"host": host}
        optional_env_map = {
            "TERADATA_DATABASE": "database",
            "TERADATA_USER": "user",
            "TERADATA_PASSWORD": "password",
            "TERADATA_LOGMECH": "logmech",
            "TERADATA_ENCRYPTDATA": "encryptdata",
            "TERADATA_TMODE": "tmode",
            "TERADATA_LOGDATA": "logdata",
        }
        for env_name, connect_name in optional_env_map.items():
            value = os.getenv(env_name)
            if value:
                connect_args[connect_name] = value

        extra_params = os.getenv("TERADATA_EXTRA_PARAMS", "{}").strip() or "{}"
        extra = json.loads(extra_params)
        if not isinstance(extra, dict):
            raise ValueError("TERADATA_EXTRA_PARAMS must be a JSON object")
        connect_args.update(extra)

        LOG.info("Connecting to Teradata host '%s' with logmech '%s'", host, connect_args.get("logmech", "default"))
        self._connection = teradatasql.connect(**connect_args)
        return self._connection

    def iter_batches(self, sql: str, fetch_size: int) -> Iterator[List[Dict[str, Any]]]:
        cursor = self.connect().cursor()
        try:
            LOG.info("Executing Teradata query: %s", sql)
            cursor.execute(sql)
            columns = [description[0] for description in cursor.description]
            while True:
                rows = cursor.fetchmany(fetch_size)
                if not rows:
                    break
                yield [normalize_row(columns, row) for row in rows]
        finally:
            cursor.close()

    def describe_class_source(self, class_mapping: ClassMapping, inline_properties: Sequence[PropertyMapping]) -> Dict[str, Any]:
        return {"type": "sql", "sql": build_class_sql(class_mapping, inline_properties)}

    def iter_class_batches(
        self,
        class_mapping: ClassMapping,
        inline_properties: Sequence[PropertyMapping],
        fetch_size: int,
    ) -> Iterator[List[Dict[str, Any]]]:
        sql = build_class_sql(class_mapping, inline_properties)
        yield from self.iter_batches(sql, fetch_size)

    def describe_property_source(self, class_mapping: ClassMapping, prop: PropertyMapping) -> Dict[str, Any]:
        return {"type": "sql", "sql": build_property_sql(class_mapping, prop)}

    def iter_property_batches(
        self,
        class_mapping: ClassMapping,
        prop: PropertyMapping,
        fetch_size: int,
    ) -> Iterator[List[Dict[str, Any]]]:
        sql = build_property_sql(class_mapping, prop)
        yield from self.iter_batches(sql, fetch_size)

    def describe_relationship_source(
        self,
        relationship: RelationshipMapping,
        source_class: ClassMapping,
        target_class: ClassMapping,
    ) -> Dict[str, Any]:
        return {"type": "sql", "sql": build_relationship_sql(relationship, source_class, target_class)}

    def iter_relationship_batches(
        self,
        relationship: RelationshipMapping,
        source_class: ClassMapping,
        target_class: ClassMapping,
        fetch_size: int,
    ) -> Iterator[List[Dict[str, Any]]]:
        sql = build_relationship_sql(relationship, source_class, target_class)
        yield from self.iter_batches(sql, fetch_size)

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None


class CsvClient(TabularDataClient):
    def __init__(self, root_dir: str, delimiter: str = ",", encoding: str = "utf-8") -> None:
        self.root_dir = Path(root_dir)
        self.delimiter = delimiter
        self.encoding = encoding

    def _resolve_table_path(self, table: str) -> Path:
        direct = self.root_dir / table
        if direct.exists():
            return direct

        if not table.endswith(".csv"):
            direct_csv = self.root_dir / f"{table}.csv"
            if direct_csv.exists():
                return direct_csv

        if "." in table:
            leaf = table.rsplit(".", 1)[1]
            leaf_csv = self.root_dir / f"{leaf}.csv"
            if leaf_csv.exists():
                return leaf_csv

        raise FileNotFoundError(f"CSV file for table '{table}' not found under {self.root_dir}")

    def _iter_projected_batches(
        self,
        table: str,
        projections: Sequence[Tuple[str, str]],
        fetch_size: int,
    ) -> Iterator[List[Dict[str, Any]]]:
        path = self._resolve_table_path(table)
        batch: List[Dict[str, Any]] = []
        with path.open("r", encoding=self.encoding, newline="") as handle:
            reader = csv.DictReader(handle, delimiter=self.delimiter)
            if reader.fieldnames is None:
                raise ValueError(f"CSV file '{path}' does not contain a header row")
            available = set(reader.fieldnames)
            required = {source_name for source_name, _ in projections}
            missing = sorted(required - available)
            if missing:
                raise KeyError(
                    f"CSV file '{path}' is missing required column(s) {missing}. Available columns: {sorted(available)}"
                )

            for raw_row in reader:
                projected = {
                    output_name: _clean_csv_value(raw_row.get(source_name))
                    for source_name, output_name in projections
                }
                batch.append(normalize_row(list(projected.keys()), list(projected.values())))
                if len(batch) >= fetch_size:
                    yield batch
                    batch = []

        if batch:
            yield batch

    def describe_class_source(self, class_mapping: ClassMapping, inline_properties: Sequence[PropertyMapping]) -> Dict[str, Any]:
        projections = [class_mapping.source.key_column]
        projections.extend(prop.source_column for prop in inline_properties if prop.source_column)
        return {"type": "csv", "table": class_mapping.source.table, "columns": unique_preserve_order(projections)}

    def iter_class_batches(
        self,
        class_mapping: ClassMapping,
        inline_properties: Sequence[PropertyMapping],
        fetch_size: int,
    ) -> Iterator[List[Dict[str, Any]]]:
        if not class_mapping.source.table or not class_mapping.source.key_column:
            raise ValueError(f"Class '{class_mapping.iri}' requires source.table and source.key_column for CSV mode")
        projections = [(class_mapping.source.key_column, class_mapping.source.key_column)]
        projections.extend((prop.source_column, prop.source_column) for prop in inline_properties if prop.source_column)
        yield from self._iter_projected_batches(class_mapping.source.table, projections, fetch_size)

    def describe_property_source(self, class_mapping: ClassMapping, prop: PropertyMapping) -> Dict[str, Any]:
        return {
            "type": "csv",
            "table": prop.source_table or class_mapping.source.table,
            "columns": [prop.entity_key_column or class_mapping.source.key_column, prop.source_column],
        }

    def iter_property_batches(
        self,
        class_mapping: ClassMapping,
        prop: PropertyMapping,
        fetch_size: int,
    ) -> Iterator[List[Dict[str, Any]]]:
        source_table = prop.source_table or class_mapping.source.table
        entity_key_column = prop.entity_key_column or class_mapping.source.key_column
        class_key = class_mapping.source.key_column
        if not source_table or not entity_key_column or not class_key or not prop.source_column:
            raise ValueError(f"Property '{prop.iri}' does not have enough metadata for CSV mode")

        projections = [
            (entity_key_column, class_key),
            (prop.source_column, prop.source_column),
        ]
        yield from self._iter_projected_batches(source_table, projections, fetch_size)

    def describe_relationship_source(
        self,
        relationship: RelationshipMapping,
        source_class: ClassMapping,
        target_class: ClassMapping,
    ) -> Dict[str, Any]:
        columns = [source_class.source.key_column, target_class.source.key_column]
        columns.extend(prop.source_column for prop in relationship.properties if prop.source_column)
        return {"type": "csv", "table": relationship.source.table, "columns": unique_preserve_order(columns)}

    def iter_relationship_batches(
        self,
        relationship: RelationshipMapping,
        source_class: ClassMapping,
        target_class: ClassMapping,
        fetch_size: int,
    ) -> Iterator[List[Dict[str, Any]]]:
        if not relationship.source.table or not relationship.source.from_column or not relationship.source.to_column:
            raise ValueError(f"Relationship '{relationship.iri}' requires source table/from/to columns for CSV mode")
        if not source_class.source.key_column or not target_class.source.key_column:
            raise ValueError(f"Relationship '{relationship.iri}' requires source/target class key columns")

        projections = [
            (relationship.source.from_column, source_class.source.key_column),
            (relationship.source.to_column, target_class.source.key_column),
        ]
        projections.extend((prop.source_column, prop.source_column) for prop in relationship.properties if prop.source_column)
        yield from self._iter_projected_batches(relationship.source.table, projections, fetch_size)
