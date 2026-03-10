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

            os.environ["JANUSGRAPH_URL"] = "ws://localhost:8182/gremlin"
            config = kg_loader.LoaderConfig.from_file(str(mapping_path))

            self.assertEqual(config.janusgraph.url, "ws://localhost:8182/gremlin")
            self.assertEqual(config.ontology.file, str(ontology_path.resolve()))
            self.assertEqual(config.classes[0].resolved_vertex_label(), "Customer")
            self.assertEqual(config.ingestion.source_type, "teradata")

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
                    )
                ],
                relationships=[],
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
                },
            )

            plan = kg_loader.SchemaPlanner(config, ontology).build()

            self.assertIn("byExternalId", plan.vertex_indexes)
            self.assertIn("byEdgeExternalId", plan.edge_indexes)
            self.assertFalse(plan.edge_indexes["byEdgeExternalId"].unique)
            self.assertIn("Customer", plan.vertex_labels)
            self.assertIn("Account", plan.vertex_labels)
            self.assertIn("ownsAccount", plan.edge_labels)


class ScriptUtilityTests(unittest.TestCase):
    def test_management_script_uses_requested_graph_alias(self) -> None:
        script = kg_loader.build_management_script("secureGraph")
        self.assertIn("mgmt = secureGraph.openManagement()", script)
        self.assertIn("java.util.Date.class", script)
        self.assertIn("case 'DECIMAL': return Double.class", script)


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