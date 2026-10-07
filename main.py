from __future__ import annotations

import argparse
import sys

from src.pipeline import STAGE_ORDER, CausalFLPipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CausalFL: causality-driven fault localization for Java, Vul4J, and CoHiker kernel cases",
    )
    parser.add_argument(
        "--project_path",
        default=".",
        help="Path to the target project",
    )
    parser.add_argument(
        "--dataset",
        default="java",
        help="Dataset or project adapter name, for example 'java', 'vul4j', 'defects4j', or 'cohiker'",
    )
    parser.add_argument(
        "--project_id",
        help="Dataset-specific project identifier",
    )
    parser.add_argument(
        "--bug_id",
        help="Dataset-specific bug identifier",
    )
    parser.add_argument(
        "--test_case_id",
        help="Optional single test case selector, for example 'org.example.MyTest::testMethod'",
    )
    parser.add_argument(
        "--output_root",
        help="Shared output root (default: output/); contains log/<dataset>/<case>/ and results/<dataset>/",
    )
    parser.add_argument(
        "--preprocess_dir",
        help="Override the default log/<dataset>/<case>/preprocess directory",
    )
    parser.add_argument(
        "--localization_dir",
        help="Override the default log/<dataset>/<case>/localization directory",
    )
    parser.add_argument(
        "--result_dir",
        help="Directory for <case>_ranking.json files (default: output/results/<dataset>)",
    )
    parser.add_argument(
        "--stage",
        default="bootstrap",
        choices=(*STAGE_ORDER, "all"),
        help=(
            "Pipeline stage to run. Public stages are bootstrap, project, preprocess, and localization. "
            "Use 'all' to run the full public pipeline."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve configuration and project metadata without running analysis stages",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    pipeline = CausalFLPipeline.from_args(args)
    summary = pipeline.run(stage=args.stage)
    print(summary.to_console())
    return 0 if summary.succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
