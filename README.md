# Enterprise ontology to JanusGraph loader

This workspace contains a production-style loader that:

- parses an OWL/RDFS ontology,
- reasons over supported subclass, equivalent-class, subproperty, inverse-property, and cardinality semantics,
- validates the ontology-to-physical mapping,
- reads source data from either CSV files or Teradata,
- creates missing JanusGraph schema objects idempotently,
- upserts vertices, properties, and edges into JanusGraph,
- supports secure JanusGraph deployments that use HBase + ZooKeeper with Kerberos and SSL.

The main entry point remains `kg_loader.py`, which is now a thin wrapper around the modular `kg_loader/` package.

For the full operational guide, see `docs/USAGE_GUIDE.md`.

## What the loader automates

Given:

- an enterprise ontology file (`.owl`, `.rdf`, `.ttl`, `.xml`, `.jsonld`, etc.), and
- a mapping file that tells the loader which ontology classes/properties map to which Teradata tables/columns,

the loader will:

1. parse ontology classes, datatype properties, and object properties,
2. create missing vertex labels, edge labels, property keys, and indexes in JanusGraph,
3. load individuals for each mapped class,
4. load datatype/attribute properties,
5. load object-property relationships as edges,
6. make reruns idempotent through stable external IDs.

## Secure topology note

There is one small-but-important architecture detail here:

- `kg_loader.py` connects to **JanusGraph / Gremlin Server**.
- **HBase backend, ZooKeeper SSL, and JanusGraph-side Kerberos** are configured on the server itself.

That is why this repo includes `config/janusgraph-hbase-secure.properties.template`.

In other words, the Python loader handles ingestion; the JanusGraph server handles secure storage/backend integration. Clean separation, less chaos.

## Files

- `kg_loader.py` — thin CLI shim kept for backward compatibility.
- `kg_loader/` — modular loader package (`config.py`, `ontology.py`, `schema.py`, `data_clients.py`, `gremlin.py`, `loader.py`, `cli.py`, etc.).
- `backups/kg_loader.monolith.backup.20260310.py` — backup of the pre-refactor monolithic implementation.
- `run_kg_loader.sh` — Kerberos-aware wrapper that uses `~/anaconda3/bin/python3`.
- `config/ontology_mapping.sample.yaml` — original sample ontology/physical mapping.
- `config/ontology_mapping.test.yaml` — local CSV + in-memory JanusGraph test profile.
- `config/ontology_mapping.prod.template.yaml` — production Teradata + JanusGraph profile template.
- `config/janusgraph-hbase-secure.properties.template` — secure JanusGraph server config baseline.
- `ontology/banking.ttl` — sample banking ontology for local testing.
- `data/banking_csv/*.csv` — generated banking sample tables (13 CSV files).
- `scripts/generate_banking_sample_data.py` — creates the banking CSV dataset.
- `scripts/install_janusgraph_local.sh` — downloads and unpacks JanusGraph `1.1.0` locally.
- `scripts/start_janusgraph_test.sh` / `scripts/stop_janusgraph_test.sh` — manage the local in-memory JanusGraph server.
- `scripts/run_local_test_mode.sh` — runs the full local test-mode workflow.
- `scripts/run_graph_validation_tests.sh` — reloads the local graph and runs the full local Python test suite.
- `scripts/verify_graph_summary.py` — prints vertex/edge counts from the loaded graph.
- `.env` — placeholder environment values you should fill in.
- `tests/test_kg_loader.py` — lightweight regression tests.

## Expected mapping model

The mapping file supports:

- top-level `ingestion`
  - `mode`: `test` or `prod`
  - `source_type`: `csv` or `teradata`
  - `csv_root_dir` / delimiter / encoding for local CSV mode
- `classes[]`
  - ontology class IRI
  - JanusGraph vertex label
  - source table or SQL
  - business key column
  - optional `id_template`
  - attribute/property mappings
- `relationships[]`
  - ontology object-property IRI
  - source class IRI
  - target class IRI
  - relationship table or SQL
  - source/target foreign key columns
  - optional `id_template`
  - edge property mappings

For external attribute tables, set `source_table` and `entity_key_column` on the property mapping.

The mapping file is treated as **trusted administrative input**. To reduce surprises, the loader rejects multi-statement SQL, SQL comments, and unsafe statement separators in generated fragments and custom `source.sql` blocks.

## Install

Use the requested Python interpreter:

```bash
~/anaconda3/bin/python3 -m pip install -r requirements.txt
```

## Configure

1. Fill in `.env`.
2. For local CSV testing, use `config/ontology_mapping.test.yaml` as-is or tailor the sample banking ontology/data.
3. For production, copy `config/ontology_mapping.prod.template.yaml` and replace the ontology IRIs, Teradata tables, and columns with your enterprise mappings.
4. If your JanusGraph server uses secure HBase/ZooKeeper, adapt `config/janusgraph-hbase-secure.properties.template` on the server host.

## Run

Dry-run first:

```bash
./run_kg_loader.sh --mapping config/ontology_mapping.sample.yaml --dry-run --report-file load_report.dryrun.json
```

Actual load:

```bash
./run_kg_loader.sh --mapping config/ontology_mapping.sample.yaml --report-file load_report.json
```

You can also run Python directly:

```bash
~/anaconda3/bin/python3 kg_loader.py --mapping config/ontology_mapping.sample.yaml
```

Or as a module:

```bash
~/anaconda3/bin/python3 -m kg_loader --mapping config/ontology_mapping.sample.yaml
```

### Local test mode

This repo includes a complete local test flow using:

- JanusGraph `1.1.0`
- the bundled in-memory backend configuration
- CSV files as the source system
- the sample banking ontology and 13 banking tables

Run the whole local workflow with:

```bash
bash scripts/run_local_test_mode.sh
```

That script will:

1. generate the banking CSV data,
2. install JanusGraph locally under `tools/`,
3. start Gremlin Server with the bundled in-memory configuration,
4. load the graph using `config/ontology_mapping.test.yaml`,
5. print vertex and edge counts from the loaded graph.

To stop the local JanusGraph server later:

```bash
bash scripts/stop_janusgraph_test.sh
```

## Design choices

### Why Python?

Python is the better fit here because the workflow needs all of the following in one place:

- ontology parsing,
- structured config loading,
- Teradata extraction,
- Gremlin/JanusGraph schema creation,
- batching, logging, validation, and reporting.

A pure shell solution would turn into a sprawling pile of quoting problems and sadness. Python keeps the logic explicit and testable.

### Idempotency strategy

- vertices are upserted by `external_id`
- edges are upserted by `edge_external_id`
- schema creation checks for existing labels/keys/indexes before creating them

Within each JanusGraph write batch, repeated vertex IDs or edge IDs are merged before submission so duplicate rows in the same batch do not create avoidable duplicates.

### Important constraint for custom templates

For relationship rows and external property rows, the loader must be able to reconstruct the source/target vertex external IDs from the data returned by the SQL query.

That means either:

- keep `id_template` based on the class key column only, or
- if you use a more complex `id_template`, provide custom SQL that returns all referenced placeholders using matching aliases.

For generated SQL paths, the loader validates this early and fails fast when a template references placeholders that are not actually returned by the corresponding query.

### Batch sizing

`runtime.batch_size` controls how many rows are sent to Gremlin Server in one request. Start conservatively (for example `100` to `500`) and increase only after observing actual request latencies and payload sizes in your environment.

### JanusGraph data type note

The loader accepts a logical `Decimal` mapping type, but JanusGraph does not support `BigDecimal` property keys directly. During JanusGraph schema/value writes, logical `Decimal` values are materialized as `Double` values.

## Validation and tests

The included tests cover:

- config loading and environment substitution,
- ontology reasoning and ontology-aware mapping validation,
- CSV source projection and alias handling,
- schema plan generation,
- query builder behavior,
- template rendering,
- live JanusGraph validation of subclass typing, domain/range compatibility, edge lineage metadata, and supported OWL restrictions.

Run them with:

```bash
~/anaconda3/bin/python3 -m unittest discover -s tests -p 'test_*.py'
```
