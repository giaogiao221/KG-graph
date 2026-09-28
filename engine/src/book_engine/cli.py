from __future__ import annotations

import argparse
import json
from pathlib import Path

from book_engine.pipeline.orchestrator import RunContext, run_pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generic book extraction engine")
    parser.add_argument("--input-md", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--mode", default="production")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    input_path = Path(args.input_md)
    config_path = Path(args.config)
    output_dir = Path(args.output_dir)

    if not input_path.exists():
        raise FileNotFoundError(f"Input file does not exist: {input_path}")
    if not config_path.exists():
        raise FileNotFoundError(f"Config file does not exist: {config_path}")

    report = run_pipeline(
        RunContext(
            input_path=input_path,
            output_dir=output_dir,
            config_path=config_path,
            mode=args.mode,
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())