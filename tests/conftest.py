"""
Purpose: Provide shared pytest bootstrap helpers for repository-local test execution.
Scope: Ensure the repository root is importable so tests can load the apps/ and services/ packages.
Dependencies: Python's sys.path handling and the repository directory layout.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
