#!/usr/bin/env python3
"""推广台入口。在仓库根目录执行：python run.py"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
