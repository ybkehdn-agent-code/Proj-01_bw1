"""pytest 가 프로젝트 루트(`engine.py` 가 있는 곳)를 import 경로에 넣도록 한다."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
