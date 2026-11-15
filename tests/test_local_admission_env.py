# SPDX-License-Identifier: MIT
# The launcher gate that forwards Local admission tuning to the proxy: only ASCII-digit
# values inside the documented bounds pass through; set-but-invalid values are skipped
# with a diagnostic so the proxy keeps its own default (audit M1).
import contextlib
import io
import json
import os
import unittest
from unittest import mock

from openrouterAdvisor import _LOCAL_ADMISSION_LIMITS, _local_admission_env

BOUNDS = {"ROUTE_LOCAL_CONCURRENCY": (1, 64), "ROUTE_LOCAL_QUEUE_LIMIT": (0, 256),
          "ROUTE_LOCAL_QUEUE_WAIT_MS": (0, 600000), "ROUTE_LOCAL_ATTEMPT_TIMEOUT_MS": (30000, 900000)}


class LocalAdmissionEnvTests(unittest.TestCase):
    def test_no_values_forward_nothing(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(_local_admission_env(), {})
            self.assertEqual(err.getvalue(), "")

    def test_bounds_match_the_proxy_validation_table(self):
        self.assertEqual({name: (low, high) for name, low, high in _LOCAL_ADMISSION_LIMITS}, BOUNDS)

    def test_edge_values_forward_verbatim(self):
        for name, (low, high) in BOUNDS.items():
            with self.subTest(name=name), mock.patch.dict(os.environ, {name: str(low)}, clear=True):
                self.assertEqual(_local_admission_env(), {name: str(low)})
            with self.subTest(name=name, edge="high"), mock.patch.dict(os.environ, {name: str(high)}, clear=True):
                self.assertEqual(_local_admission_env(), {name: str(high)})

    def test_out_of_range_values_are_skipped_with_a_diagnostic(self):
        for name, (low, high) in BOUNDS.items():
            for value in (str(low - 1), str(high + 1)):
                with self.subTest(name=name, value=value), mock.patch.dict(os.environ, {name: value}, clear=True):
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err):
                        self.assertEqual(_local_admission_env(), {})
                    diagnostic = json.loads(err.getvalue())
                    self.assertEqual(diagnostic["event"], "local_admission_env_ignored")
                    self.assertEqual(diagnostic["name"], name)

    def test_malformed_values_are_skipped_including_unicode_digits(self):
        # '²'.isdigit() is True and int('６５') parses, so ASCII is checked explicitly; a
        # digit-only string past Python's int-conversion limit must still be skipped.
        for name in BOUNDS:
            for value in ("", "invalid", "-5", "1.5", "²", "６５", "9" * 5000):
                with self.subTest(name=name, value=value[:12]), mock.patch.dict(os.environ, {name: value}, clear=True):
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err):
                        self.assertEqual(_local_admission_env(), {})
                    diagnostic = json.loads(err.getvalue())
                    self.assertEqual(diagnostic["event"], "local_admission_env_ignored")
                    self.assertEqual(diagnostic["name"], name)

    def test_valid_and_invalid_mix_forwards_only_the_valid_one(self):
        with mock.patch.dict(os.environ,
                             {"ROUTE_LOCAL_CONCURRENCY": "4", "ROUTE_LOCAL_QUEUE_LIMIT": "bad"}, clear=True):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(_local_admission_env(), {"ROUTE_LOCAL_CONCURRENCY": "4"})
            self.assertIn("ROUTE_LOCAL_QUEUE_LIMIT", err.getvalue())

    def test_explicit_mapping_is_used_when_passed(self):
        self.assertEqual(_local_admission_env({"ROUTE_LOCAL_CONCURRENCY": "6"}), {"ROUTE_LOCAL_CONCURRENCY": "6"})
        self.assertEqual(_local_admission_env({"ROUTE_LOCAL_CONCURRENCY": "99"}), {})

    def test_leading_zeros_forward_as_the_number(self):
        with mock.patch.dict(os.environ, {"ROUTE_LOCAL_QUEUE_WAIT_MS": "007"}, clear=True):
            self.assertEqual(_local_admission_env(), {"ROUTE_LOCAL_QUEUE_WAIT_MS": "007"})


if __name__ == "__main__":
    unittest.main()
