"""
Put ``python_bridge/`` on ``sys.path``.

The bridge modules import each other flatly (``from thinkgear import ...``)
because they are run as scripts, not installed as a package. Every test file
imports this module first so it works the same under

    pytest
    python -m unittest discover -s tests
    python tests/test_signal_processor.py
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE_DIR = os.path.join(REPO_ROOT, "python_bridge")
TESTS_DIR = os.path.join(REPO_ROOT, "tests")

for _path in (BRIDGE_DIR, TESTS_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)
