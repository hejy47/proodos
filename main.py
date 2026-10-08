from __future__ import annotations

import argparse
import sys

from src.pipeline import STAGE_ORDER, DebugPipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Debug pipeline: evidence-guided diagnosis and automated repair",
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
        "--result_dir",
        default="output",
        help="Root directory for case logs, preprocessing data, debug reports, and patches (default: output)",
    )
    parser.add_argument(
        "--stage",
        default="all",
        choices=(*STAGE_ORDER, "all"),
        help=(
            "Pipeline stage to run. Public stages are preprocess and debug. "
            "Use 'all' to run the full public pipeline."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    pipeline = DebugPipeline.from_args(args)
    summary = pipeline.run(stage=args.stage)
    print(summary.to_console())
    return 0 if summary.succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
