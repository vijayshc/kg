import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import kg_loader


class LoaderConfigTests(unittest.TestCase):
    def test_env_substitution_and_relative_ontology_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            ontology_path = tmp_path / "enterprise.owl"
            ontology_path.write_text("<rdf:RDF></rdf:RDF>", encoding="utf-8")

            mapping = {
                "ontology": {"file": "./enterprise.owl"},
                "janusgraph": {"url": "${JANUSGRAPH_URL}"},
                "runtime": {"batch_size": 100},
                "indexes": [
                    {
                        "name": "customerByName",
                        "element": "vertex",
                        "kind": "composite",
                        "property_keys": ["customerName"],
                        "index_only": "Customer",
                    }
                ],
                "classes": [
                    {
                        "iri": "https://example.com/Customer",
                        "source": {"table": "EDW.customer_dim", "key_column": "customer_id"},
                        "properties": [
                            {
                                "iri": "https://example.com/customerName",
                                "source_column": "customer_name",
                                "meta_properties": [
                                    {
                                        "property_key": "propertyOrigin",
                                        "constant_value": "customer_dim.customer_name",
                                        "data_type": "String",
                                    }
                                ],
                            }
                        ],
                    }
                ],
                "relationships": [],
            }
            mapping_path = tmp_path / "mapping.json"
            mapping_path.write_text(json.dumps(mapping), encoding="utf-8")

            os.environ["JANUSGRAPH_URL"] = "ws://localhost:8182/gremlin"
            config = kg_loader.LoaderConfig.from_file(str(mapping_path))

            self.assertEqual(config.janusgraph.url, "ws://localhost:8182/gremlin")
            self.assertEqual(config.ontology.file, str(ontology_path.resolve()))
            self.assertEqual(config.classes[0].resolved_vertex_label(), "Customer")
            self.assertEqual(config.ingestion.source_type, "teradata")
            self.assertEqual(len(config.indexes), 1)
            self.assertEqual(config.indexes[0].name, "customerByName")
            self.assertEqual(config.indexes[0].property_keys, ["customerName"])
            self.assertEqual(config.classes[0].properties[0].meta_properties[0].property_key, "propertyOrigin")

    def test_validation_rejects_external_property_template_missing_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            ontology_path = tmp_path / "enterprise.owl"
            ontology_path.write_text("<rdf:RDF></rdf:RDF>", encoding="utf-8")

            mapping = {
                "ontology": {"file": "./enterprise.owl"},
                "janusgraph": {"url": "ws://localhost:8182/gremlin"},
                "classes": [
                    {
                        "iri": "https://example.com/Customer",
                        "id_template": "Customer:{customer_id}:{country_code}",
                        "source": {"table": "EDW.customer_dim", "key_column": "customer_id"},
                        "properties": [
                            {
                                "iri": "https://example.com/customerSegment",
                                "source_column": "segment_code",
                                "source_table": "EDW.customer_segment_hist",
                                "entity_key_column": "customer_id",
                            }
                        ],
                    }
                ],
                "relationships": [],
            }
            mapping_path = tmp_path / "mapping.json"
            mapping_path.write_text(json.dumps(mapping), encoding="utf-8")

            with self.assertRaises(ValueError) as ctx:
                kg_loader.LoaderConfig.from_file(str(mapping_path))

            self.assertIn("country_code", str(ctx.exception))
            self.assertIn("template", str(ctx.exception))

    def test_validation_rejects_invalid_janusgraph_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            ontology_path = tmp_path / "enterprise.owl"
            ontology_path.write_text("<rdf:RDF></rdf:RDF>", encoding="utf-8")

            mapping = {
                "ontology": {"file": "./enterprise.owl"},
                "janusgraph": {"url": "http://localhost:8182/gremlin"},
                "classes": [
                    {
                        "iri": "https://example.com/Customer",
                        "source": {"table": "EDW.customer_dim", "key_column": "customer_id"},
                        "properties": [
                            {
                                "iri": "https://example.com/customerName",
                                "source_column": "customer_name",
                            }
                        ],
                    }
                ],
                "relationships": [],
            }
            mapping_path = tmp_path / "mapping.json"
            mapping_path.write_text(json.dumps(mapping), encoding="utf-8")

            with self.assertRaises(ValueError) as ctx:
                kg_loader.LoaderConfig.from_file(str(mapping_path))

            self.assertIn("ws:// or wss://", str(ctx.exception))

    def test_validation_rejects_invalid_custom_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            ontology_path = tmp_path / "enterprise.owl"
            ontology_path.write_text("<rdf:RDF></rdf:RDF>", encoding="utf-8")

            mapping = {
                "ontology": {"file": "./enterprise.owl"},
                "janusgraph": {"url": "ws://localhost:8182/gremlin"},
                "indexes": [
                    {
                        "name": "badMixedIndex",
                        "element": "vertex",
                        "kind": "mixed",
                        "unique": True,
                        "property_keys": ["customerName"],
                    }
                ],
                "classes": [
                    {
                        "iri": "https://example.com/Customer",
                        "source": {"table": "EDW.customer_dim", "key_column": "customer_id"},
                        "properties": [
                            {
                                "iri": "https://example.com/customerName",
                                "source_column": "customer_name",
                            }
                        ],
                    }
                ],
                "relationships": [],
            }
            mapping_path = tmp_path / "mapping.json"
            mapping_path.write_text(json.dumps(mapping), encoding="utf-8")

            with self.assertRaises(ValueError) as ctx:
                kg_loader.LoaderConfig.from_file(str(mapping_path))

            self.assertIn("Mixed index", str(ctx.exception))

    def test_validation_rejects_relationship_property_meta_properties(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            ontology_path = tmp_path / "enterprise.owl"
            ontology_path.write_text("<rdf:RDF></rdf:RDF>", encoding="utf-8")

            mapping = {
                "ontology": {"file": "./enterprise.owl"},
                "janusgraph": {"url": "ws://localhost:8182/gremlin"},
                "classes": [
                    {
                        "iri": "https://example.com/Customer",
                        "source": {"table": "customer_dim", "key_column": "customer_id"},
                    },
                    {
                        "iri": "https://example.com/Account",
                        "source": {"table": "account_dim", "key_column": "account_id"},
                    },
                ],
                "relationships": [
                    {
                        "iri": "https://example.com/ownsAccount",
                        "source_class_iri": "https://example.com/Customer",
                        "target_class_iri": "https://example.com/Account",
                        "source": {"table": "bridge", "from_column": "customer_id", "to_column": "account_id"},
                        "properties": [
                            {
                                "iri": "https://example.com/relationshipType",
                                "source_column": "relationship_type",
                                "meta_properties": [
                                    {
                                        "property_key": "badMeta",
                                        "constant_value": "oops",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
            mapping_path = tmp_path / "mapping.json"
            mapping_path.write_text(json.dumps(mapping), encoding="utf-8")

            with self.assertRaises(ValueError) as ctx:
                kg_loader.LoaderConfig.from_file(str(mapping_path))

            self.assertIn("cannot define meta_properties", str(ctx.exception))

    def test_table_column_shorthand_infers_external_property_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            ontology_path = tmp_path / "enterprise.owl"
            ontology_path.write_text("<rdf:RDF></rdf:RDF>", encoding="utf-8")

            mapping = {
                "ontology": {"file": "./enterprise.owl"},
                "janusgraph": {"url": "ws://localhost:8182/gremlin"},
                "classes": [
                    {
                        "iri": "https://example.com/Customer",
                        "source": {"table": "EDW.customer_dim", "key_column": "customer_id"},
                        "properties": [
                            {"iri": "https://example.com/customerName", "source_column": "EDW.customer_dim.customer_name"},
                            {"iri": "https://example.com/city", "source_column": "EDW.customer_address.city"},
                            {
                                "iri": "https://example.com/postalCode",
                                "source_column": "EDW.customer_address.postal_code",
                                "entity_key_column": "EDW.customer_address.customer_id",
                            },
                        ],
                    }
                ],
                "relationships": [],
            }
            mapping_path = tmp_path / "mapping.json"
            mapping_path.write_text(json.dumps(mapping), encoding="utf-8")

            config = kg_loader.LoaderConfig.from_file(str(mapping_path))

            name_prop, city_prop, postal_prop = config.classes[0].properties
            self.assertEqual(name_prop.source_column, "customer_name")
            self.assertIsNone(name_prop.source_table)
            self.assertEqual(city_prop.source_column, "city")
            self.assertEqual(city_prop.source_table, "EDW.customer_address")
            self.assertEqual(postal_prop.entity_key_column, "customer_id")
            self.assertEqual(postal_prop.source_table, "EDW.customer_address")

    def test_table_comma_column_shorthand_is_supported_for_properties(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            ontology_path = tmp_path / "enterprise.owl"
            ontology_path.write_text("<rdf:RDF></rdf:RDF>", encoding="utf-8")

            mapping = {
                "ontology": {"file": "./enterprise.owl"},
                "janusgraph": {"url": "ws://localhost:8182/gremlin"},
                "classes": [
                    {
                        "iri": "https://example.com/Customer",
                        "source": {"table": "EDW.customer_dim", "key_column": "customer_id"},
                        "properties": [
                            {"iri": "https://example.com/customerName", "source_column": "EDW.customer_dim,customer_name"},
                            {"iri": "https://example.com/city", "source_column": "EDW.customer_address,city"},
                            {
                                "iri": "https://example.com/postalCode",
                                "source_column": "EDW.customer_address,postal_code",
                                "entity_key_column": "EDW.customer_address,customer_id",
                            },
                        ],
                    }
                ],
                "relationships": [],
            }
            mapping_path = tmp_path / "mapping.json"
            mapping_path.write_text(json.dumps(mapping), encoding="utf-8")

            config = kg_loader.LoaderConfig.from_file(str(mapping_path))

            name_prop, city_prop, postal_prop = config.classes[0].properties
            self.assertEqual(name_prop.source_column, "customer_name")
            self.assertIsNone(name_prop.source_table)
            self.assertEqual(city_prop.source_column, "city")
            self.assertEqual(city_prop.source_table, "EDW.customer_address")
            self.assertEqual(postal_prop.entity_key_column, "customer_id")
            self.assertEqual(postal_prop.source_table, "EDW.customer_address")

    def test_table_column_shorthand_can_infer_relationship_source_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            ontology_path = tmp_path / "enterprise.owl"
            ontology_path.write_text("<rdf:RDF></rdf:RDF>", encoding="utf-8")

            mapping = {
                "ontology": {"file": "./enterprise.owl"},
                "janusgraph": {"url": "ws://localhost:8182/gremlin"},
                "classes": [
                    {"iri": "https://example.com/Customer", "source": {"table": "customer_dim", "key_column": "customer_id"}},
                    {"iri": "https://example.com/Account", "source": {"table": "account_dim", "key_column": "account_id"}},
                ],
                "relationships": [
                    {
                        "iri": "https://example.com/ownsAccount",
                        "source_class_iri": "https://example.com/Customer",
                        "target_class_iri": "https://example.com/Account",
                        "source": {
                            "from_column": "EDW.account_customer_bridge.customer_id",
                            "to_column": "EDW.account_customer_bridge.account_id",
                        },
                        "properties": [
                            {
                                "iri": "https://example.com/relationshipType",
                                "source_column": "EDW.account_customer_bridge.relationship_type",
                            }
                        ],
                    }
                ],
            }
            mapping_path = tmp_path / "mapping.json"
            mapping_path.write_text(json.dumps(mapping), encoding="utf-8")

            config = kg_loader.LoaderConfig.from_file(str(mapping_path))

            relationship = config.relationships[0]
            self.assertEqual(relationship.source.table, "EDW.account_customer_bridge")
            self.assertEqual(relationship.source.from_column, "customer_id")
            self.assertEqual(relationship.source.to_column, "account_id")
            self.assertEqual(relationship.properties[0].source_column, "relationship_type")

    def test_validation_rejects_relationship_shortcuts_spanning_multiple_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            ontology_path = tmp_path / "enterprise.owl"
            ontology_path.write_text("<rdf:RDF></rdf:RDF>", encoding="utf-8")

            mapping = {
                "ontology": {"file": "./enterprise.owl"},
                "janusgraph": {"url": "ws://localhost:8182/gremlin"},
                "classes": [
                    {"iri": "https://example.com/Customer", "source": {"table": "customer_dim", "key_column": "customer_id"}},
                    {"iri": "https://example.com/Account", "source": {"table": "account_dim", "key_column": "account_id"}},
                ],
                "relationships": [
                    {
                        "iri": "https://example.com/ownsAccount",
                        "source_class_iri": "https://example.com/Customer",
                        "target_class_iri": "https://example.com/Account",
                        "source": {
                            "from_column": "EDW.customer_dim.customer_id",
                            "to_column": "EDW.account_dim.account_id",
                        },
                    }
                ],
            }
            mapping_path = tmp_path / "mapping.json"
            mapping_path.write_text(json.dumps(mapping), encoding="utf-8")

            with self.assertRaises(ValueError) as ctx:
                kg_loader.LoaderConfig.from_file(str(mapping_path))

            self.assertIn("Use custom SQL", str(ctx.exception))


class QueryBuilderTests(unittest.TestCase):
    def test_build_relationship_sql_aliases_foreign_keys(self) -> None:
        customer = kg_loader.ClassMapping(
            iri="https://example.com/Customer",
            source=kg_loader.SourceSpec(table="EDW.customer_dim", key_column="customer_id"),
        )
        account = kg_loader.ClassMapping(
            iri="https://example.com/Account",
            source=kg_loader.SourceSpec(table="EDW.account_dim", key_column="account_id"),
        )
        relationship = kg_loader.RelationshipMapping(
            iri="https://example.com/ownsAccount",
            source_class_iri=customer.iri,
            target_class_iri=account.iri,
            source=kg_loader.SourceSpec(
                table="EDW.customer_account_bridge",
                from_column="cust_id",
                to_column="acct_id",
            ),
            properties=[
                kg_loader.PropertyMapping(
                    iri="https://example.com/relationshipType",
                    source_column="relationship_type",
                )
            ],
        )

        sql = kg_loader.build_relationship_sql(relationship, customer, account)

        self.assertIn("cust_id AS customer_id", sql)
        self.assertIn("acct_id AS account_id", sql)
        self.assertIn("relationship_type", sql)

    def test_render_template_is_case_insensitive_for_normalized_rows(self) -> None:
        row = kg_loader.normalize_row(["CUSTOMER_ID"], [42])
        rendered = kg_loader.render_template("Customer:{customer_id}", row)
        self.assertEqual(rendered, "Customer:42")


class SchemaPlannerTests(unittest.TestCase):
    def test_schema_plan_contains_indexes_and_unmapped_ontology_terms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            ontology_file = tmp_path / "dummy.owl"
            ontology_file.write_text("<rdf:RDF></rdf:RDF>", encoding="utf-8")

            config = kg_loader.LoaderConfig(
                ontology=kg_loader.OntologyDefinition(file=str(ontology_file), include_unmapped_terms=True),
                janusgraph=kg_loader.JanusGraphSettings(url="ws://localhost:8182/gremlin"),
                runtime=kg_loader.RuntimeSettings(),
                ingestion=kg_loader.IngestionSettings(mode="test", source_type="csv", csv_root_dir=str(tmp_path)),
                classes=[
                    kg_loader.ClassMapping(
                        iri="https://example.com/Customer",
                        source=kg_loader.SourceSpec(table="EDW.customer_dim", key_column="customer_id"),
                        properties=[
                            kg_loader.PropertyMapping(
                                iri="https://example.com/customerName",
                                source_column="customer_name",
                            )
                        ],
                    ),
                    kg_loader.ClassMapping(
                        iri="https://example.com/Account",
                        source=kg_loader.SourceSpec(table="EDW.account_dim", key_column="account_id"),
                        properties=[
                            kg_loader.PropertyMapping(
                                iri="https://example.com/currentBalance",
                                property_key="balanceSnapshot",
                                source_column="current_balance",
                                data_type="Decimal",
                                cardinality="LIST",
                                meta_properties=[
                                    kg_loader.MetaPropertyMapping(
                                        property_key="balanceRecordedAt",
                                        source_column="open_date",
                                        data_type="Date",
                                    )
                                ],
                            )
                        ],
                    )
                ],
                indexes=[
                    kg_loader.GraphIndexDefinition(
                        name="customerByNameAndOntology",
                        element="vertex",
                        kind="composite",
                        property_keys=["customerName", "ontology_iri"],
                        index_only="Customer",
                    )
                ],
                edge_relation_indexes=[
                    kg_loader.EdgeRelationIndexDefinition(
                        name="ownsAccountByRelationshipTypeVc",
                        edge_label="ownsAccount",
                        direction="OUT",
                        sort_order="asc",
                        property_keys=["relationshipType"],
                    )
                ],
                property_relation_indexes=[
                    kg_loader.PropertyRelationIndexDefinition(
                        name="balanceSnapshotByRecordedAt",
                        property_key="balanceSnapshot",
                        sort_order="desc",
                        meta_property_keys=["balanceRecordedAt"],
                    )
                ],
                relationships=[
                    kg_loader.RelationshipMapping(
                        iri="https://example.com/ownsAccount",
                        source_class_iri="https://example.com/Customer",
                        target_class_iri="https://example.com/Account",
                        source=kg_loader.SourceSpec(table="bridge", from_column="customer_id", to_column="account_id"),
                        properties=[
                            kg_loader.PropertyMapping(
                                iri="https://example.com/relationshipType",
                                source_column="relationship_type",
                                data_type="String",
                            )
                        ],
                    )
                ],
                mapping_file="memory",
            )

            ontology = kg_loader.OntologyCatalog(
                classes={
                    "https://example.com/Customer": kg_loader.OntologyClass("https://example.com/Customer"),
                    "https://example.com/Account": kg_loader.OntologyClass("https://example.com/Account"),
                },
                properties={
                    "https://example.com/customerName": kg_loader.OntologyProperty(
                        iri="https://example.com/customerName",
                        kind="datatype",
                        ranges={"http://www.w3.org/2001/XMLSchema#string"},
                    ),
                    "https://example.com/ownsAccount": kg_loader.OntologyProperty(
                        iri="https://example.com/ownsAccount",
                        kind="object",
                    ),
                    "https://example.com/currentBalance": kg_loader.OntologyProperty(
                        iri="https://example.com/currentBalance",
                        kind="datatype",
                        ranges={"http://www.w3.org/2001/XMLSchema#decimal"},
                    ),
                    "https://example.com/relationshipType": kg_loader.OntologyProperty(
                        iri="https://example.com/relationshipType",
                        kind="datatype",
                        ranges={"http://www.w3.org/2001/XMLSchema#string"},
                    ),
                },
            )

            plan = kg_loader.SchemaPlanner(config, ontology).build()

            self.assertIn("byExternalId", plan.vertex_indexes)
            self.assertIn("byEdgeExternalId", plan.edge_indexes)
            self.assertFalse(plan.edge_indexes["byEdgeExternalId"].unique)
            self.assertEqual(
                plan.vertex_indexes["customerByNameAndOntology"].property_keys,
                ("customerName", "ontology_iri"),
            )
            self.assertEqual(plan.vertex_indexes["customerByNameAndOntology"].index_only, "Customer")
            self.assertIn("balanceRecordedAt", plan.property_keys)
            self.assertIn("Customer", plan.vertex_labels)
            self.assertIn("Account", plan.vertex_labels)
            self.assertIn("ownsAccount", plan.edge_labels)
            self.assertIn("Customer", plan.vertex_property_constraints)
            self.assertIn("ownsAccount", plan.edge_property_constraints)
            self.assertIn("ownsAccount:Customer:Account", plan.connection_constraints)
            self.assertIn("balanceSnapshotByRecordedAt", plan.property_relation_indexes)
            self.assertEqual(
                plan.property_relation_indexes["balanceSnapshotByRecordedAt"].meta_property_keys,
                ("balanceRecordedAt",),
            )
            self.assertIn("ownsAccountByRelationshipTypeVc", plan.edge_relation_indexes)


class RecommendationTests(unittest.TestCase):
    def test_query_pattern_recommendations_mark_existing_and_missing_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            ontology_path = tmp_path / "enterprise.owl"
            ontology_path.write_text("<rdf:RDF></rdf:RDF>", encoding="utf-8")

            config = kg_loader.LoaderConfig(
                ontology=kg_loader.OntologyDefinition(file=str(ontology_path), include_unmapped_terms=False),
                janusgraph=kg_loader.JanusGraphSettings(url="ws://localhost:8182/gremlin"),
                runtime=kg_loader.RuntimeSettings(),
                ingestion=kg_loader.IngestionSettings(mode="test", source_type="csv", csv_root_dir=str(tmp_path)),
                classes=[
                    kg_loader.ClassMapping(
                        iri="https://example.com/Customer",
                        source=kg_loader.SourceSpec(table="customer_dim", key_column="customer_id"),
                        properties=[
                            kg_loader.PropertyMapping(
                                iri="https://example.com/customerName",
                                source_column="customer_name",
                                data_type="String",
                            ),
                            kg_loader.PropertyMapping(
                                iri="https://example.com/email",
                                source_column="email",
                                data_type="String",
                            ),
                        ],
                    ),
                    kg_loader.ClassMapping(
                        iri="https://example.com/Account",
                        source=kg_loader.SourceSpec(table="account_dim", key_column="account_id"),
                        properties=[
                            kg_loader.PropertyMapping(
                                iri="https://example.com/currentBalance",
                                property_key="balanceSnapshot",
                                source_column="current_balance",
                                data_type="Decimal",
                                cardinality="LIST",
                                meta_properties=[
                                    kg_loader.MetaPropertyMapping(
                                        property_key="balanceRecordedAt",
                                        source_column="open_date",
                                        data_type="Date",
                                    )
                                ],
                            )
                        ],
                    ),
                    kg_loader.ClassMapping(
                        iri="https://example.com/Transaction",
                        source=kg_loader.SourceSpec(table="txn", key_column="transaction_id"),
                    ),
                ],
                relationships=[
                    kg_loader.RelationshipMapping(
                        iri="https://example.com/postedTransaction",
                        source_class_iri="https://example.com/Account",
                        target_class_iri="https://example.com/Transaction",
                        edge_label="postedTransaction",
                        source=kg_loader.SourceSpec(table="txn", from_column="account_id", to_column="transaction_id"),
                        properties=[
                            kg_loader.PropertyMapping(
                                iri="https://example.com/debitCredit",
                                source_column="debit_credit",
                                data_type="String",
                            ),
                            kg_loader.PropertyMapping(
                                iri="https://example.com/transactionDate",
                                property_key="transactionDateEdge",
                                source_column="transaction_date",
                                data_type="Date",
                            ),
                        ],
                    )
                ],
                indexes=[
                    kg_loader.GraphIndexDefinition(
                        name="customerByEmail",
                        element="vertex",
                        property_keys=["email"],
                        kind="composite",
                        index_only="Customer",
                    )
                ],
                edge_relation_indexes=[
                    kg_loader.EdgeRelationIndexDefinition(
                        name="postedTransactionsByDebitAndDate",
                        edge_label="postedTransaction",
                        direction="OUT",
                        sort_order="desc",
                        property_keys=["debitCredit", "transactionDateEdge"],
                    )
                ],
                property_relation_indexes=[
                    kg_loader.PropertyRelationIndexDefinition(
                        name="balanceSnapshotByRecordedAt",
                        property_key="balanceSnapshot",
                        sort_order="desc",
                        meta_property_keys=["balanceRecordedAt"],
                    )
                ],
                query_patterns=[
                    kg_loader.QueryPatternDefinition(
                        name="Customer Email Lookup",
                        pattern_type="graph",
                        element="vertex",
                        label="Customer",
                        predicates=[kg_loader.QueryPredicateDefinition(property_key="email", operator="eq")],
                    ),
                    kg_loader.QueryPatternDefinition(
                        name="Customer Name Search",
                        pattern_type="graph",
                        element="vertex",
                        label="Customer",
                        predicates=[kg_loader.QueryPredicateDefinition(property_key="customerName", operator="textcontains")],
                    ),
                    kg_loader.QueryPatternDefinition(
                        name="Recent Debit Transactions By Account",
                        pattern_type="traversal",
                        start_label="Account",
                        edge_label="postedTransaction",
                        direction="OUT",
                        predicates=[
                            kg_loader.QueryPredicateDefinition(property_key="debitCredit", operator="eq"),
                            kg_loader.QueryPredicateDefinition(property_key="transactionDateEdge", operator="range"),
                        ],
                        order_by="transactionDateEdge",
                        order="desc",
                    ),
                    kg_loader.QueryPatternDefinition(
                        name="Current Balance By Recorded At",
                        pattern_type="property_meta",
                        property_key="balanceSnapshot",
                        predicates=[kg_loader.QueryPredicateDefinition(property_key="balanceRecordedAt", operator="range")],
                        order_by="balanceRecordedAt",
                        order="desc",
                    ),
                ],
                mapping_file="memory",
            )

            ontology = kg_loader.OntologyCatalog(classes={}, properties={})
            schema_plan = kg_loader.SchemaPlanner(config, ontology).build()
            recommendations = kg_loader.recommend_query_pattern_indexes(config, schema_plan)
            by_name = {item.name: item for item in recommendations}

            self.assertEqual(by_name["Customer_Email_Lookup"].status, "satisfied")
            self.assertEqual(by_name["Recent_Debit_Transactions_By_Account"].status, "satisfied")
            self.assertEqual(by_name["Current_Balance_By_Recorded_At"].status, "satisfied")
            self.assertEqual(by_name["Customer_Name_Search"].status, "recommended")
            self.assertEqual(by_name["Customer_Name_Search"].details["kind"], "mixed")

    def test_auto_indexes_recommendations_and_multiplicity_inference_work_without_manual_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            ontology_path = tmp_path / "enterprise.owl"
            ontology_path.write_text("<rdf:RDF></rdf:RDF>", encoding="utf-8")

            config = kg_loader.LoaderConfig(
                ontology=kg_loader.OntologyDefinition(file=str(ontology_path), include_unmapped_terms=False),
                janusgraph=kg_loader.JanusGraphSettings(url="ws://localhost:8182/gremlin"),
                runtime=kg_loader.RuntimeSettings(),
                ingestion=kg_loader.IngestionSettings(mode="test", source_type="csv", csv_root_dir=str(tmp_path)),
                classes=[
                    kg_loader.ClassMapping(
                        iri="https://example.com/Customer",
                        source=kg_loader.SourceSpec(table="customer_dim", key_column="customer_id"),
                        properties=[
                            kg_loader.PropertyMapping(
                                iri="https://example.com/email",
                                source_column="email",
                                data_type="String",
                            )
                        ],
                    ),
                    kg_loader.ClassMapping(
                        iri="https://example.com/Account",
                        source=kg_loader.SourceSpec(table="account_dim", key_column="account_id"),
                        properties=[
                            kg_loader.PropertyMapping(
                                iri="https://example.com/currentBalance",
                                property_key="balanceSnapshot",
                                source_column="current_balance",
                                data_type="Decimal",
                                cardinality="LIST",
                                meta_properties=[
                                    kg_loader.MetaPropertyMapping(
                                        property_key="balanceRecordedAt",
                                        source_column="open_date",
                                        data_type="Date",
                                    )
                                ],
                            )
                        ],
                    ),
                    kg_loader.ClassMapping(
                        iri="https://example.com/Transaction",
                        source=kg_loader.SourceSpec(table="transaction_fact", key_column="transaction_id"),
                    ),
                ],
                relationships=[
                    kg_loader.RelationshipMapping(
                        iri="https://example.com/postedTransaction",
                        source_class_iri="https://example.com/Account",
                        target_class_iri="https://example.com/Transaction",
                        source=kg_loader.SourceSpec(
                            table="transaction_fact",
                            from_column="account_id",
                            to_column="transaction_id",
                        ),
                        properties=[
                            kg_loader.PropertyMapping(
                                iri="https://example.com/transactionDate",
                                property_key="transactionDateEdge",
                                source_column="transaction_date",
                                data_type="Date",
                            )
                        ],
                    )
                ],
                mapping_file="memory",
            )

            ontology = kg_loader.OntologyCatalog(
                classes={},
                properties={
                    "https://example.com/postedTransaction": kg_loader.OntologyProperty(
                        iri="https://example.com/postedTransaction",
                        kind="object",
                        characteristics={"inverse_functional"},
                    )
                },
            )

            schema_plan = kg_loader.SchemaPlanner(config, ontology).build()
            self.assertIn("auto_vertex_Customer_email", schema_plan.vertex_indexes)
            self.assertIn("auto_edge_postedTransaction_transactionDateEdge", schema_plan.edge_indexes)
            self.assertIn("auto_edge_rel_postedTransaction_transactionDateEdge", schema_plan.edge_relation_indexes)
            self.assertIn("auto_property_rel_balanceSnapshot_balanceRecordedAt", schema_plan.property_relation_indexes)
            self.assertEqual(schema_plan.edge_labels["postedTransaction"].multiplicity, "ONE2MANY")

            recommendations = kg_loader.recommend_query_pattern_indexes(config, schema_plan)
            by_name = {item.name: item for item in recommendations}
            self.assertEqual(by_name["Customer_email_Exact_Lookup"].status, "satisfied")
            self.assertEqual(by_name["postedTransaction_transactionDateEdge_Incident_Traversal"].status, "satisfied")
            self.assertEqual(by_name["balanceSnapshot_balanceRecordedAt_Meta_Traversal"].status, "satisfied")


class ScriptUtilityTests(unittest.TestCase):
    def test_management_script_uses_requested_graph_alias(self) -> None:
        script = kg_loader.build_management_script("secureGraph")
        self.assertIn("mgmt = secureGraph.openManagement()", script)
        self.assertIn("java.util.Date.class", script)
        self.assertIn("case 'DECIMAL': return Double.class", script)
        self.assertIn("buildMixedIndex", script)
        self.assertIn("createdGraphIndexes", script)
        self.assertIn("buildEdgeIndex", script)
        self.assertIn("buildPropertyIndex", script)
        self.assertIn("addConnection", script)
        self.assertIn("ParameterType", script)


class CsvClientTests(unittest.TestCase):
    def test_csv_client_projects_and_aliases_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            csv_path = tmp_path / "customer_account_bridge.csv"
            csv_path.write_text(
                "cust_id,acct_id,relationship_type\nC001,A100,PRIMARY\n",
                encoding="utf-8",
            )

            client = kg_loader.CsvClient(str(tmp_path))
            customer = kg_loader.ClassMapping(
                iri="https://example.com/Customer",
                source=kg_loader.SourceSpec(table="customer_dim", key_column="customer_id"),
            )
            account = kg_loader.ClassMapping(
                iri="https://example.com/Account",
                source=kg_loader.SourceSpec(table="account_dim", key_column="account_id"),
            )
            relationship = kg_loader.RelationshipMapping(
                iri="https://example.com/ownsAccount",
                source_class_iri=customer.iri,
                target_class_iri=account.iri,
                source=kg_loader.SourceSpec(
                    table="customer_account_bridge",
                    from_column="cust_id",
                    to_column="acct_id",
                ),
                properties=[
                    kg_loader.PropertyMapping(
                        iri="https://example.com/relationshipType",
                        source_column="relationship_type",
                    )
                ],
            )

            batches = list(client.iter_relationship_batches(relationship, customer, account, fetch_size=10))

            self.assertEqual(len(batches), 1)
            self.assertEqual(batches[0][0]["customer_id"], "C001")
            self.assertEqual(batches[0][0]["account_id"], "A100")
            self.assertEqual(batches[0][0]["relationship_type"], "PRIMARY")


class DeduplicationTests(unittest.TestCase):
    def test_vertex_payload_rows_are_merged_by_external_id(self) -> None:
        rows = [
            {
                "external_id": "Customer:C001",
                "properties": [{"key": "customerName", "value": "Alice"}],
            },
            {
                "external_id": "Customer:C001",
                "properties": [{"key": "email", "value": "alice@example.com"}],
            },
        ]

        merged = kg_loader.deduplicate_payload_rows(rows, "external_id")

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["external_id"], "Customer:C001")
        self.assertEqual([item["key"] for item in merged[0]["properties"]], ["customerName", "email"])

    def test_edge_payload_rows_are_merged_by_edge_external_id(self) -> None:
        rows = [
            {
                "edge_external_id": "ownsAccount:C001:A100",
                "properties": [{"key": "relationshipType", "value": "PRIMARY"}],
            },
            {
                "edge_external_id": "ownsAccount:C001:A100",
                "properties": [{"key": "channel", "value": "ONLINE"}],
            },
        ]

        merged = kg_loader.deduplicate_payload_rows(rows, "edge_external_id")

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["edge_external_id"], "ownsAccount:C001:A100")
        self.assertEqual([item["key"] for item in merged[0]["properties"]], ["relationshipType", "channel"])


class OntologyReasoningTests(unittest.TestCase):
    def test_ontology_reasoning_captures_subclasses_properties_and_restrictions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            ontology_path = tmp_path / "reasoning.ttl"
            ontology_path.write_text(
                """
@prefix ex: <https://example.com/ontology#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

ex:Party a owl:Class .
ex:Customer a owl:Class ; rdfs:subClassOf ex:Party .
ex:Client a owl:Class ; owl:equivalentClass ex:Customer .
ex:Account a owl:Class .

ex:ownsProduct a owl:ObjectProperty ; rdfs:domain ex:Party ; rdfs:range ex:Account .
ex:ownsAccount a owl:ObjectProperty ; rdfs:subPropertyOf ex:ownsProduct ; owl:inverseOf ex:accountOwnedBy .
ex:accountRelationship a owl:ObjectProperty .
ex:accountOwnedBy a owl:ObjectProperty ; owl:inverseOf ex:ownsAccount ; rdfs:subPropertyOf ex:accountRelationship .
ex:managerFor a owl:ObjectProperty, owl:FunctionalProperty .

ex:Customer rdfs:subClassOf [
  a owl:Restriction ;
  owl:onProperty ex:ownsAccount ;
  owl:minQualifiedCardinality "1"^^xsd:nonNegativeInteger ;
  owl:onClass ex:Account
] .
                """.strip(),
                encoding="utf-8",
            )

            ontology = kg_loader.load_ontology(kg_loader.OntologyDefinition(file=str(ontology_path), format="turtle"))

            self.assertIn("https://example.com/ontology#Party", ontology.class_lineage("https://example.com/ontology#Customer"))
            self.assertIn("https://example.com/ontology#Client", ontology.class_lineage("https://example.com/ontology#Customer"))
            self.assertIn(
                "https://example.com/ontology#ownsProduct",
                ontology.property_lineage("https://example.com/ontology#ownsAccount"),
            )
            self.assertIn(
                "https://example.com/ontology#accountOwnedBy",
                ontology.inverse_properties_for("https://example.com/ontology#ownsAccount"),
            )
            self.assertNotIn(
                "https://example.com/ontology#accountRelationship",
                ontology.inverse_properties_for("https://example.com/ontology#ownsAccount"),
            )
            self.assertIn(
                "functional",
                ontology.effective_property_characteristics("https://example.com/ontology#managerFor"),
            )
            restrictions = ontology.restrictions_for_class("https://example.com/ontology#Customer")
            self.assertTrue(
                any(
                    item.property_iri == "https://example.com/ontology#ownsAccount"
                    and item.constraint_type == "min_cardinality"
                    and item.cardinality == 1
                    for item in restrictions
                )
            )


class OntologyValidationTests(unittest.TestCase):
    def test_subclass_and_superproperty_domain_range_validation_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            ontology_path = tmp_path / "ontology.ttl"
            ontology_path.write_text(
                """
@prefix ex: <https://example.com/ontology#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

ex:Party a owl:Class .
ex:FinancialProduct a owl:Class .
ex:Customer a owl:Class ; rdfs:subClassOf ex:Party .
ex:Account a owl:Class ; rdfs:subClassOf ex:FinancialProduct .
ex:ownsProduct a owl:ObjectProperty ; rdfs:domain ex:Party ; rdfs:range ex:FinancialProduct .
ex:ownsAccount a owl:ObjectProperty ; rdfs:subPropertyOf ex:ownsProduct ; rdfs:domain ex:Customer ; rdfs:range ex:Account .
                """.strip(),
                encoding="utf-8",
            )

            config = kg_loader.LoaderConfig(
                ontology=kg_loader.OntologyDefinition(file=str(ontology_path), format="turtle"),
                janusgraph=kg_loader.JanusGraphSettings(url="ws://localhost:8182/gremlin"),
                runtime=kg_loader.RuntimeSettings(strict_ontology=True),
                ingestion=kg_loader.IngestionSettings(mode="test", source_type="csv", csv_root_dir=str(tmp_path)),
                classes=[
                    kg_loader.ClassMapping(
                        iri="https://example.com/ontology#Customer",
                        source=kg_loader.SourceSpec(table="customer_dim", key_column="customer_id"),
                    ),
                    kg_loader.ClassMapping(
                        iri="https://example.com/ontology#Account",
                        source=kg_loader.SourceSpec(table="account_dim", key_column="account_id"),
                    ),
                ],
                relationships=[
                    kg_loader.RelationshipMapping(
                        iri="https://example.com/ontology#ownsAccount",
                        source_class_iri="https://example.com/ontology#Customer",
                        target_class_iri="https://example.com/ontology#Account",
                        multiplicity="MULTI",
                        source=kg_loader.SourceSpec(table="bridge", from_column="customer_id", to_column="account_id"),
                    )
                ],
                mapping_file="memory",
            )

            ontology = kg_loader.load_ontology(config.ontology)
            kg_loader.validate_mapping_against_ontology(config, ontology)

    def test_functional_relationship_rejects_non_functional_multiplicity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            ontology_path = tmp_path / "ontology.ttl"
            ontology_path.write_text(
                """
@prefix ex: <https://example.com/ontology#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

ex:Account a owl:Class .
ex:Branch a owl:Class .
ex:issuedByBranch a owl:ObjectProperty, owl:FunctionalProperty ;
  rdfs:domain ex:Account ;
  rdfs:range ex:Branch .
                """.strip(),
                encoding="utf-8",
            )

            config = kg_loader.LoaderConfig(
                ontology=kg_loader.OntologyDefinition(file=str(ontology_path), format="turtle"),
                janusgraph=kg_loader.JanusGraphSettings(url="ws://localhost:8182/gremlin"),
                runtime=kg_loader.RuntimeSettings(strict_ontology=True),
                ingestion=kg_loader.IngestionSettings(mode="test", source_type="csv", csv_root_dir=str(tmp_path)),
                classes=[
                    kg_loader.ClassMapping(
                        iri="https://example.com/ontology#Account",
                        source=kg_loader.SourceSpec(table="account_dim", key_column="account_id"),
                    ),
                    kg_loader.ClassMapping(
                        iri="https://example.com/ontology#Branch",
                        source=kg_loader.SourceSpec(table="branch_dim", key_column="branch_id"),
                    ),
                ],
                relationships=[
                    kg_loader.RelationshipMapping(
                        iri="https://example.com/ontology#issuedByBranch",
                        source_class_iri="https://example.com/ontology#Account",
                        target_class_iri="https://example.com/ontology#Branch",
                        multiplicity="MULTI",
                        source=kg_loader.SourceSpec(table="account_dim", from_column="account_id", to_column="branch_id"),
                    )
                ],
                mapping_file="memory",
            )

            ontology = kg_loader.load_ontology(config.ontology)

            with self.assertRaises(ValueError) as ctx:
                kg_loader.validate_mapping_against_ontology(config, ontology)

            self.assertIn("MANY2ONE or ONE2ONE", str(ctx.exception))

    def test_all_values_from_rejects_incompatible_relationship_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            ontology_path = tmp_path / "ontology.ttl"
            ontology_path.write_text(
                """
@prefix ex: <https://example.com/ontology#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

ex:Account a owl:Class .
ex:Branch a owl:Class .
ex:Employee a owl:Class .
ex:issuedByBranch a owl:ObjectProperty .

ex:Account rdfs:subClassOf [
  a owl:Restriction ;
  owl:onProperty ex:issuedByBranch ;
  owl:allValuesFrom ex:Branch
] .
                """.strip(),
                encoding="utf-8",
            )

            config = kg_loader.LoaderConfig(
                ontology=kg_loader.OntologyDefinition(file=str(ontology_path), format="turtle"),
                janusgraph=kg_loader.JanusGraphSettings(url="ws://localhost:8182/gremlin"),
                runtime=kg_loader.RuntimeSettings(strict_ontology=True),
                ingestion=kg_loader.IngestionSettings(mode="test", source_type="csv", csv_root_dir=str(tmp_path)),
                classes=[
                    kg_loader.ClassMapping(
                        iri="https://example.com/ontology#Account",
                        source=kg_loader.SourceSpec(table="account_dim", key_column="account_id"),
                    ),
                    kg_loader.ClassMapping(
                        iri="https://example.com/ontology#Employee",
                        source=kg_loader.SourceSpec(table="employee_dim", key_column="employee_id"),
                    ),
                ],
                relationships=[
                    kg_loader.RelationshipMapping(
                        iri="https://example.com/ontology#issuedByBranch",
                        source_class_iri="https://example.com/ontology#Account",
                        target_class_iri="https://example.com/ontology#Employee",
                        multiplicity="MANY2ONE",
                        source=kg_loader.SourceSpec(table="account_dim", from_column="account_id", to_column="employee_id"),
                    )
                ],
                mapping_file="memory",
            )

            ontology = kg_loader.load_ontology(config.ontology)

            with self.assertRaises(ValueError) as ctx:
                kg_loader.validate_mapping_against_ontology(config, ontology)

            self.assertIn("constrained to target class", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()