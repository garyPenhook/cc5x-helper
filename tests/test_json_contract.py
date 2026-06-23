"""Regression tests: commands must hold their contract on bad input, not traceback.

Audit (round 2, #1): malformed pack `.ini` raised `configparser.ParsingError` (not a
ValueError/OSError) and several `--json` commands loaded the manifest *before* their
protective handler, so a bad manifest / pack escaped as a raw traceback with empty stdout.
"""

from __future__ import annotations

import json
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path

import cc5x_setcc_native as build
from cc5x_setcc_native_lib.picmeta import parse_ini_text


class ConfigParserNormalizationTests(unittest.TestCase):
    def test_malformed_ini_raises_valueerror_not_parsingerror(self) -> None:
        # A line with no section header makes configparser raise MissingSectionHeaderError
        # (a configparser.Error, NOT a ValueError). It must be normalized so main()/the JSON
        # boundary handle it like any other bad input.
        with self.assertRaises(ValueError):
            parse_ini_text("garbage line with no section header\n")


class BuildJsonDiagnosticsContractTests(unittest.TestCase):
    def test_malformed_manifest_emits_json_not_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "setcc-native.json"
            path.write_text("{ this is not valid json", encoding="utf-8")
            args = types.SimpleNamespace(
                project=str(path), edition=None, dry_run=False, json_diagnostics=True,
                timeout_seconds=0,
            )
            with unittest.mock.patch("builtins.print") as printed:
                rc = build.cmd_build(args)
            self.assertEqual(rc, 1)
            payload = json.loads("".join(str(c.args[0]) for c in printed.call_args_list))
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["kind"], "build_not_ready")


class ProgramJsonContractTests(unittest.TestCase):
    def test_missing_manifest_emits_json_via_boundary(self) -> None:
        # cmd_program loads the manifest before its internal _program_error handling, so the
        # registered handler is json_error_boundary(cmd_program); a missing --project must
        # still produce parseable JSON.
        wrapped = build.json_error_boundary(build.cmd_program)
        args = types.SimpleNamespace(
            action="program", ipecmd=None, device=None, hex=None,
            project="/no/such/manifest.json", edition=None, tool="PK4",
            release_from_reset=False, ipe_arg=[], dry_run=False, json=True,
            timeout_seconds=0,
        )
        with unittest.mock.patch("builtins.print") as printed:
            rc = wrapped(args)
        self.assertEqual(rc, 1)
        payload = json.loads("".join(str(c.args[0]) for c in printed.call_args_list))
        self.assertFalse(payload["ok"])

    def test_program_handler_is_wrapped_by_boundary(self) -> None:
        # Pin the wiring so a future refactor can't silently drop the boundary.
        parser = build.build_parser()
        args = parser.parse_args(
            ["program", "--project", "/no/such/manifest.json", "--device", "PIC16F1509",
             "--json", "--action", "program", "--hex", "/no/such.hex"]
        )
        with unittest.mock.patch("builtins.print") as printed:
            rc = args.func(args)
        self.assertEqual(rc, 1)
        payload = json.loads("".join(str(c.args[0]) for c in printed.call_args_list))
        self.assertFalse(payload["ok"])


def _program_args(**overrides: object) -> types.SimpleNamespace:
    base = dict(
        action="erase", ipecmd="/fake/ipecmd", device="PIC16F1509", hex=None,
        project=None, edition=None, tool="PK4", release_from_reset=False, ipe_arg=[],
        dry_run=False, json=True, timeout_seconds=0, yes=False,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


class ProgramConfirmationTests(unittest.TestCase):
    """Device-modifying actions must be confirmed; generated tasks can no longer flash silently."""

    def test_destructive_without_yes_is_refused_and_does_not_run(self) -> None:
        args = _program_args(action="erase", yes=False, json=True)
        with unittest.mock.patch.object(build.subprocess, "run") as run, \
                unittest.mock.patch("builtins.print") as printed:
            rc = build.cmd_program(args)
        run.assert_not_called()
        self.assertEqual(rc, 1)
        payload = json.loads("".join(str(c.args[0]) for c in printed.call_args_list))
        self.assertEqual(payload["error"]["kind"], "confirmation_required")

    def test_yes_authorizes_destructive_action(self) -> None:
        args = _program_args(action="erase", yes=True, json=True)
        completed = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        with unittest.mock.patch.object(build.subprocess, "run", return_value=completed) as run, \
                unittest.mock.patch("builtins.print"):
            rc = build.cmd_program(args)
        run.assert_called_once()
        self.assertEqual(rc, 0)

    def test_dry_run_destructive_needs_no_confirmation(self) -> None:
        args = _program_args(action="erase", yes=False, dry_run=True, json=True)
        with unittest.mock.patch.object(build.subprocess, "run") as run, \
                unittest.mock.patch("builtins.print"):
            rc = build.cmd_program(args)
        run.assert_not_called()
        self.assertEqual(rc, 0)

    def test_non_destructive_action_runs_without_yes(self) -> None:
        args = _program_args(action="blank-check", yes=False, json=True)
        completed = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        with unittest.mock.patch.object(build.subprocess, "run", return_value=completed) as run, \
                unittest.mock.patch("builtins.print"):
            rc = build.cmd_program(args)
        run.assert_called_once()
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
