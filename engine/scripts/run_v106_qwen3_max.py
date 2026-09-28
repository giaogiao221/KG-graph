"""运行 v106 LLM 抽取流水线（qwen3-max）。

把 demo/src 加入 sys.path 后委托给 book_engine.llm_v106.pipeline。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from book_engine.llm_v106.pipeline import main


if __name__ == "__main__":
    raise SystemExit(main())
