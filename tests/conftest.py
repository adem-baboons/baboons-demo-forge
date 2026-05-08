"""Shared test fixtures + path setup.

Adds the repo root to sys.path so ``from server import ...`` works
without an editable install.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
