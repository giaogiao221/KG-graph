"""使单元测试自包含：把 demo/src 加入 sys.path，并定位 demo 根目录。

测试文件统一以 ``DEMO_ROOT`` 取 ``src``、``config``、``scripts`` 等资源，
避免依赖旧的 ``model/`` 目录层级。
"""

from __future__ import annotations

import sys
from pathlib import Path

DEMO_ROOT = Path(__file__).resolve().parents[2]
SRC = DEMO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
