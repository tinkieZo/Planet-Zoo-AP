r"""Shared pytest fixtures for the Track A test suite.

Run the whole suite from the repo root with the **3.11 venv** (the client imports the vendored
Archipelago tree, which requires Python 3.11.9+):

    .\.venv\Scripts\python.exe -m pytest

The individual test files also still run standalone as scripts (``python tests/test_*.py``) - their
``main()`` is preserved; this conftest only adds the pytest wiring.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client import data as pz_data  # noqa: E402


@pytest.fixture
def gd():
    """The validated GameData parsed from data.json (loaded fresh per test)."""
    return pz_data.load()
