"""Path assertions shared by tests of canonical public results."""

from __future__ import annotations

import unittest
from collections.abc import Sequence
from pathlib import Path


def _canonical(value: str | Path) -> str:
    return str(Path(value).resolve(strict=False))


def assert_canonical_path(
    test_case: unittest.TestCase, actual: object, expected: str | Path
) -> None:
    test_case.assertIsInstance(actual, str)
    if not isinstance(actual, str):
        return
    test_case.assertEqual(actual, _canonical(actual), "result path is not canonical")
    test_case.assertEqual(actual, _canonical(expected))


def assert_canonical_paths(
    test_case: unittest.TestCase,
    actual: object,
    expected: Sequence[str | Path],
) -> None:
    test_case.assertIsInstance(actual, list)
    if not isinstance(actual, list):
        return
    test_case.assertEqual(len(actual), len(expected))
    for actual_path, expected_path in zip(actual, expected, strict=True):
        assert_canonical_path(test_case, actual_path, expected_path)
