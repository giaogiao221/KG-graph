from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from book_engine.llm_v106.historical_table_metadata_backfill import run_batch


DEFAULT_ALIASES = {
    "input_007": "Assessment of Joint Improvised Explosive Device Defeat Organization (JIEDDO) Training Activity",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill table provenance for historical triple exports")
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--markdown-root", type=Path, action="append", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--alias-map", type=Path)
    parser.add_argument("--file-manifest", type=Path, help="JSON array of paths relative to input-root")
    return parser


def _aliases(path: Path | None) -> dict[str, str]:
    aliases = dict(DEFAULT_ALIASES)
    if path is None:
        return aliases
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("alias-map must contain a JSON object")
    aliases.update({str(key): str(value) for key, value in payload.items() if key and value})
    return aliases


def _manifest(path: Path | None) -> list[Path] | None:
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
        raise ValueError("file-manifest must contain a JSON array of relative file paths")
    return [Path(item) for item in payload]


def main() -> int:
    args = _parser().parse_args()
    report = run_batch(
        args.input_root,
        args.markdown_root,
        args.output_root,
        _aliases(args.alias_map),
        included_files=_manifest(args.file_manifest),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
