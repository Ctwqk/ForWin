from __future__ import annotations

import sys
import builtins
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.postgres import postgres_test_url

builtins.postgres_test_url = postgres_test_url
