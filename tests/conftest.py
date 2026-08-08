"""Pytest hook: make the bridge modules importable from the test suite."""

import _bootstrap  # noqa: F401  (import for its side effect)
