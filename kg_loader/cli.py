from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Optional, Sequence

from .config import LoaderConfig
from .loader import KnowledgeGraphLoader
from .utils import load_optional_dotenv, setup_logging

LOG = logging.getLogger("kg_loader")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load ontology-driven data from Teradata into JanusGraph")
    parser.add_argument("--mapping", required=True, help="Path to YAML or JSON mapping file")
    parser.add_argument("--ontology", help="Optional override for ontology file path")
    parser.add_argument("--dry-run", action="store_true", help="Validate config and print schema/SQL plan only")
    parser.add_argument("--strict-ontology", action="store_true", help="Fail if mappings reference unknown ontology terms")
    parser.add_argument("--report-file", help="Optional output path for JSON load report")
    parser.add_argument("--log-level", default="INFO", help="Logging level (DEBUG, INFO, WARNING, ERROR)")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(args.log_level)
    load_optional_dotenv()

    try:
        config = LoaderConfig.from_file(
            file_path=args.mapping,
            ontology_override=args.ontology,
            dry_run_override=args.dry_run,
            strict_override=True if args.strict_ontology else None,
        )
        loader = KnowledgeGraphLoader(config)
        report = loader.run()
        report_json = json.dumps(report, indent=2, sort_keys=True)
        if args.report_file:
            Path(args.report_file).write_text(report_json + "\n", encoding="utf-8")
            LOG.info("Wrote report to %s", args.report_file)
        print(report_json)
        return 0
    except Exception as exc:  # pragma: no cover - CLI safety net
        LOG.exception("Loader failed: %s", exc)
        return 1
