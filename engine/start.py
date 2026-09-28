"""Demo 启动器：v105 本地规则抽取 → v106 LLM 抽取。

在 demo 目录运行：python start.py
默认流程：单元测试 → v105 本地抽取 → v106 生成计划 → v106 正式抽取。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

# demo 目录即项目根；数据内置在 demo/books，产物写到 demo/runs
PROJECT_ROOT = Path(__file__).resolve().parent
BOOKS_DIR = PROJECT_ROOT / "books"
RUNS_ROOT = PROJECT_ROOT / "runs"
TEST_TIMEOUT_SECONDS = 60
ENV_ALIASES = {
    "MODEL": "OPENAI_MODEL",
    "BASE_URL": "OPENAI_BASE_URL",
    "API_KEY": "OPENAI_API_KEY",
}
V105_ENV = {
    "KGCHOUQU_PRODUCTION_PROFILE": "balanced",
    "KGCHOUQU_PROPERTY_LLM_ENABLED": "0",
    "KGCHOUQU_PRODUCTION_LLM_ENABLED": "0",
    "KGCHOUQU_SUBJECT_LLM_ENABLED": "0",
    "KGCHOUQU_SEMANTIC_SPLIT_LLM_ENABLED": "0",
    "KGCHOUQU_PROCESS_LLM_ENABLED": "0",
    "KGCHOUQU_TABLE_AXIS_LLM_ENABLED": "0",
}
V105_OUTPUTS = (
    "step_graph_guard/graph_import_ready_high_precision.tsv",
    "step_graph_guard/graph_import_ready_generalized.tsv",
    "step_graph_guard/schema59_validation_report.json",
    "book_engine_run_report.json",
)


class StartupError(RuntimeError):
    pass


def with_env_aliases(values: Mapping[str, str]) -> dict[str, str]:
    result = dict(values)
    for alias, standard in ENV_ALIASES.items():
        if alias in result and standard not in result:
            result[standard] = result[alias]
    return result


def load_environment(path: Path, inherited: Mapping[str, str]) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.is_file():
        for number, line in enumerate(
            path.read_text(encoding="utf-8-sig").splitlines(), 1
        ):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.fullmatch(
                r"(?:\$env:|export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line
            )
            if match is None:
                raise StartupError(f"Invalid environment assignment: {path}:{number}")
            # 配置按纯文本解析，不执行命令或变量替换。
            lexer = shlex.shlex(match[2], posix=True)
            lexer.whitespace_split = True
            lexer.escape = ""
            try:
                tokens = list(lexer)
            except ValueError:
                raise StartupError(
                    f"Unclosed environment value: {path}:{number}"
                ) from None
            if len(tokens) > 1 or any("\x00" in token for token in tokens):
                raise StartupError(
                    f"Invalid environment value: {path}:{number}; quote values containing spaces"
                )
            values[match[1]] = tokens[0] if tokens else ""

    # 每个来源分别归一化别名，进程环境优先于文件。
    env = {**with_env_aliases(values), **with_env_aliases(inherited)}
    src = str(PROJECT_ROOT / "src")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (src, env.get("PYTHONPATH", ""))))
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--books-dir", type=Path, default=BOOKS_DIR)
    parser.add_argument(
        "--v105-root", type=Path, default=RUNS_ROOT / "v105_generalized"
    )
    # 产物与缓存目录默认跟随 OPENAI_MODEL 命名，便于按模型区分抽取数据。
    parser.add_argument(
        "--v106-root",
        type=Path,
        default=None,
        help="v106 产物根目录；默认 runs/v106_<OPENAI_MODEL>",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="v106 LLM 缓存目录；默认 llm_cache_v106/<OPENAI_MODEL>",
    )
    parser.add_argument(
        "--python",
        type=Path,
        help="Python 解释器路径；默认用项目 .venv，其次当前解释器",
    )
    parser.add_argument(
        "--skip-tests", action="store_true", help="跳过启动前的单元测试"
    )
    parser.add_argument(
        "--skip-v106", action="store_true", help="只运行 v105 本地抽取"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="校验输入并打印将执行的命令，不实际执行",
    )
    return parser


def resolve_python(explicit: Path | None) -> Path:
    if explicit:
        python = explicit
    elif (PROJECT_ROOT / ".venv").exists():
        python = (
            PROJECT_ROOT
            / ".venv"
            / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
    else:
        python = Path(sys.executable)
    # 不解析 venv 符号链接，避免选到基础解释器。
    python = python.absolute()
    if not python.is_file():
        raise StartupError(
            f"Python interpreter not found: {python}; use --python to select one"
        )
    return python


def v105_complete(output_dir: Path) -> bool:
    if not all((output_dir / name).is_file() for name in V105_OUTPUTS):
        return False
    report_path = output_dir / V105_OUTPUTS[2]
    try:
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, UnicodeError):
        raise StartupError(f"Invalid JSON report: {report_path}") from None
    if not isinstance(report, dict) or type(report.get("ok")) is not bool:
        raise StartupError(f"Report must contain a boolean 'ok': {report_path}")
    return report["ok"]


def run_command(
    label: str,
    command: Sequence[str],
    env: Mapping[str, str],
    *,
    dry_run: bool,
    timeout: int | None = None,
) -> None:
    print(f"[{label}]", flush=True)
    if dry_run:
        print(subprocess.list2cmdline(command), flush=True)
        return
    subprocess.run(command, cwd=PROJECT_ROOT, env=env, check=True, timeout=timeout)


def run(args: argparse.Namespace) -> None:
    python = resolve_python(args.python)
    env = load_environment(PROJECT_ROOT / "config.env", os.environ)
    skip_v106 = args.skip_v106 or env.get("KGCHOUQU_SKIP_V106") == "1"
    books_dir = args.books_dir.resolve()
    v105_root = args.v105_root.resolve()
    # 未显式指定时，按当前模型名派生 v106 产物与缓存目录。
    model_name = env.get("OPENAI_MODEL", "").strip() or "default"
    v106_root = (
        args.v106_root.resolve()
        if args.v106_root
        else (RUNS_ROOT / f"v106_{model_name}").resolve()
    )
    cache_dir = (
        args.cache_dir.resolve()
        if args.cache_dir
        else (PROJECT_ROOT / "llm_cache_v106" / model_name).resolve()
    )
    policy = PROJECT_ROOT / "src" / "config" / "strict_generic_policy.yaml"
    ontology = PROJECT_ROOT / "src" / "config" / "property_ontology_v2.tsv"
    v105_runner = PROJECT_ROOT / "src" / "book_engine" / "cli.py"
    v106_runner = PROJECT_ROOT / "scripts" / "run_v106_qwen3_max.py"
    required_files = [
        v105_runner,
        policy,
        PROJECT_ROOT / "src" / "config" / "legacy_schema59_contract.json",
    ]
    if not skip_v106:
        required_files.extend((v106_runner, ontology))
        missing = [
            name for name in ENV_ALIASES.values() if not env.get(name, "").strip()
        ]
        if missing:
            raise StartupError(
                f"Configure {', '.join(missing)} in config.env or use --skip-v106"
            )
    for path in required_files:
        if not path.is_file():
            raise StartupError(f"Required file not found: {path}")
    if not books_dir.is_dir():
        raise StartupError(f"Books directory not found: {books_dir}")
    books = sorted(
        (
            path
            for path in books_dir.iterdir()
            if path.is_file() and path.suffix.lower() == ".md"
        ),
        key=lambda path: path.name.casefold(),
    )
    if not books:
        raise StartupError(f"No Markdown books found: {books_dir}")
    output_dirs = [
        v105_root / re.sub(r'[\\/:*?"<>|]', "_", book.stem) for book in books
    ]
    if len({str(path).casefold() for path in output_dirs}) != len(output_dirs):
        raise StartupError(
            "Book filenames map to duplicate output directories; rename them before running"
        )

    print(
        f"[PYTHON] {python}\n[BOOKS] {books_dir}\n[TOTAL] {len(books)} books",
        flush=True,
    )
    prefix = [str(python), "-u"]
    if not args.skip_tests:
        tests = PROJECT_ROOT / "tests" / "unit"
        run_command(
            "TESTS",
            [*prefix, "-m", "pytest", str(tests), "-q"],
            env,
            dry_run=args.dry_run,
            timeout=TEST_TIMEOUT_SECONDS,
        )

    local_env = {**env, **V105_ENV}
    for book, output_dir in zip(books, output_dirs):
        if v105_complete(output_dir):
            print(f"[V105 CACHE] {book.name}", flush=True)
            continue
        run_command(
            f"V105 RUN: {book.name}",
            [
                *prefix,
                str(v105_runner),
                "--input-md",
                str(book),
                "--output-dir",
                str(output_dir),
                "--config",
                str(policy),
                "--mode",
                "production",
            ],
            local_env,
            dry_run=args.dry_run,
        )
        if not args.dry_run and not v105_complete(output_dir):
            raise StartupError(
                f"v105 output missing or validation failed: {output_dir}"
            )

    if not skip_v106:
        command = [
            *prefix,
            str(v106_runner),
            "--books-dir",
            str(books_dir),
            "--v105-root",
            str(v105_root),
            "--output-root",
            str(v106_root),
            "--property-ontology",
            str(ontology),
            "--cache-dir",
            str(cache_dir),
            "--model",
            env["OPENAI_MODEL"],
        ]
        run_command("V106 PLAN", [*command, "--plan-only"], env, dry_run=args.dry_run)
        run_command("V106 EXTRACT", command, env, dry_run=args.dry_run)
    print(
        "[DRY RUN COMPLETE] No commands executed." if args.dry_run else "[COMPLETE]",
        flush=True,
    )
    print(f"v105: {v105_root}", flush=True)
    if not skip_v106:
        print(f"v106: {v106_root}", flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run(args)
    except subprocess.CalledProcessError as error:
        print(
            f"[ERROR] Stage failed (exit {error.returncode}); stopped. Keep outputs/cache to resume.",
            file=sys.stderr,
        )
        return error.returncode
    except subprocess.TimeoutExpired:
        print(
            f"[ERROR] Unit tests exceeded {TEST_TIMEOUT_SECONDS}s; extraction stopped.",
            file=sys.stderr,
        )
        return 1
    except (StartupError, OSError) as error:
        print(f"[ERROR] {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("[INTERRUPTED] Keep outputs/cache to resume.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
