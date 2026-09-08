"""Part B of the OOM checker v1 (docs/plans/2026-07-24-oom-checker-cli-v1.md):
`decoy preflight`'s capacity section.

T7 ("Revised acceptance tests"): the four capacity states (fit / insufficient
/ unknown / not-applicable) each render their own line and exit
0 / EXIT_CAPACITY / 0 / 0 -- INSUFFICIENT must exit EXIT_CAPACITY, never
preflight's generic has_failures -> EXIT_USAGE path. Also covers a relative
source-path case and the out-of-core size-threshold boundary.

`decide_execution_route`'s `out_of_core_threshold_rows` default is
5,000,000 rows; `estimate_job_capacity` exposes no override (R2's pinned
signature), so `low_threshold` monkeypatches `decoy_engine.execution.
capacity.decide_execution_route` with a thin wrapper that lowers it --
mirrors the engine-side test suite's own trick, one layer up (through the
real CLI command instead of calling the estimator directly).
"""

from __future__ import annotations

import json as _json
import os
from pathlib import Path
from typing import Any
from unittest import mock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml
from typer.testing import CliRunner

from decoy.__main__ import app
from decoy.cli.exit_codes import EXIT_CAPACITY, EXIT_OK

runner = CliRunner()

_N = 40


def _hash_col(name: str, namespace: str) -> dict[str, Any]:
    return {"name": name, "strategy": "hash", "namespace": namespace}


def _parent_child_tables(n: int = _N) -> tuple[pa.Table, pa.Table]:
    parent = pa.table(
        {
            "id": pa.array([f"p{i}" for i in range(n)], type=pa.string()),
            "note": pa.array([f"secret{i}" for i in range(n)], type=pa.string()),
        }
    )
    child = pa.table(
        {
            "cid": pa.array([f"c{i}" for i in range(n)], type=pa.string()),
            "parent_id": pa.array([f"p{i}" for i in range(n)], type=pa.string()),
        }
    )
    return parent, child


def _write_config(
    tmp_path: Path,
    *,
    relative_paths: bool = False,
    tables: tuple[pa.Table, pa.Table] | None = None,
) -> Path:
    """A pure-mask FK job (hash keys + a redact payload -- both out-of-core
    supported) with real Parquet sources, written under `tmp_path`."""
    parent, child = tables if tables is not None else _parent_child_tables()
    pq.write_table(parent, tmp_path / "parent.parquet")
    pq.write_table(child, tmp_path / "child.parquet")

    parent_path = "parent.parquet" if relative_paths else str(tmp_path / "parent.parquet")
    child_path = "child.parquet" if relative_paths else str(tmp_path / "child.parquet")

    cfg = {
        "version": 1,
        "global_settings": {"seed": 7},
        "sources": {
            "parent": {"type": "file", "path": parent_path, "format": "parquet"},
            "child": {"type": "file", "path": child_path, "format": "parquet"},
        },
        "targets": {
            "parent": {
                "type": "file",
                "path": str(tmp_path / "parent.out.parquet"),
                "format": "parquet",
            },
            "child": {
                "type": "file",
                "path": str(tmp_path / "child.out.parquet"),
                "format": "parquet",
            },
        },
        "tables": [
            {
                "name": "parent",
                "columns": [_hash_col("id", "ns"), {"name": "note", "strategy": "redact"}],
            },
            {"name": "child", "columns": [_hash_col("cid", "cns"), _hash_col("parent_id", "ns")]},
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["parent_id"]}],
                "orphan_policy": "preserve",
                "namespace": "ns",
            }
        ],
    }
    p = tmp_path / "pipeline.yaml"
    p.write_text(yaml.dump(cfg), encoding="utf-8")
    return p


def _no_relationships_config(tmp_path: Path) -> Path:
    src = tmp_path / "flat.parquet"
    pq.write_table(pa.table({"id": pa.array(range(_N))}), src)
    cfg = {
        "version": 1,
        "global_settings": {"seed": 1},
        "sources": {"flat": {"type": "file", "path": str(src), "format": "parquet"}},
        "tables": [{"name": "flat", "columns": [{"name": "id", "strategy": "passthrough"}]}],
        "targets": {
            "flat": {
                "type": "file",
                "path": str(tmp_path / "flat.out.parquet"),
                "format": "parquet",
            }
        },
    }
    p = tmp_path / "pipeline.yaml"
    p.write_text(yaml.dump(cfg), encoding="utf-8")
    return p


@pytest.fixture()
def low_threshold(monkeypatch: pytest.MonkeyPatch):
    """Lower the engine's out-of-core size thresholds so the 40-row fixture
    routes `out_of_core` instead of `sequential` -- a test-only wrapper
    around `decide_execution_route`, mirroring decoy-engine's own
    `test_lazy_path_route_admission.py` trick."""
    import decoy_engine.execution.capacity as capacity_mod

    real_decide = capacity_mod.decide_execution_route

    def _patched(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("out_of_core_threshold_rows", 10)
        kwargs.setdefault("full_frame_reject_rows", 10)
        return real_decide(*args, **kwargs)

    monkeypatch.setattr(capacity_mod, "decide_execution_route", _patched)


def _run_preflight(config_path: Path, *, json_mode: bool = False) -> Any:
    args = ["preflight", str(config_path)]
    if json_mode:
        args.append("--json")
    return runner.invoke(app, args)


def _check_status(payload: dict[str, Any], name: str) -> str | None:
    """Status of the named check in the `--json` payload's `checks` list."""
    for c in payload["checks"]:
        if c["name"] == name:
            return c["status"]
    return None


class TestFourCapacityStates:
    def test_not_applicable_no_relationships(self, tmp_path: Path) -> None:
        config_path = _no_relationships_config(tmp_path)
        result = _run_preflight(config_path)
        assert result.exit_code == EXIT_OK
        assert "capacity:" in result.output
        assert "not applicable" in result.output

    def test_ambiguous_below_size_threshold_is_not_checked_not_not_applicable(
        self, tmp_path: Path
    ) -> None:
        # No low_threshold fixture: at the real 5,000,000-row default, the
        # row-count-only decision picks `sequential` for this 40-row FK
        # fixture. But the fixture IS out-of-core-compatible, and a real
        # `decoy run` (byte-estimate routing on by default) can ignore that
        # threshold and route it to out-of-core-FK regardless of size
        # (Codex P1-1 gate finding) -- so preflight must not claim "not
        # applicable" here (a false "fine"). It renders the engine's
        # UNKNOWN verdict as "not checked" instead.
        config_path = _write_config(tmp_path)
        result = _run_preflight(config_path)
        assert result.exit_code == EXIT_OK
        assert "capacity:" in result.output
        assert "not checked" in result.output
        assert "not applicable" not in result.output

    def test_non_file_source_skips_capacity_no_network(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A "local" preflight must not open a DB/remote connection: when any
        # source is non-file, the capacity check is skipped and the engine
        # estimator (which would sample the source) is never invoked.
        import decoy_engine.execution as engine_exec

        called = {"n": 0}

        def _spy(*a: Any, **k: Any) -> Any:
            called["n"] += 1
            return None

        monkeypatch.setattr(engine_exec, "estimate_job_capacity", _spy)

        config_path = _write_config(tmp_path)
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        # A valid non-file (network) source type: s3.
        cfg["sources"]["parent"] = {
            "type": "s3",
            "format": "parquet",
            "bucket": "b",
            "key": "parent.parquet",
        }
        config_path.write_text(yaml.dump(cfg), encoding="utf-8")

        result = _run_preflight(config_path)
        assert called["n"] == 0  # gate fired before any estimate/connection
        assert "capacity:" in result.output
        assert "file sources" in result.output

    def test_fit(self, tmp_path: Path, low_threshold) -> None:
        config_path = _write_config(tmp_path)
        result = _run_preflight(config_path)
        assert result.exit_code == EXIT_OK
        assert "capacity:" in result.output
        assert "OK" in result.output

    def test_warned_exits_ok_not_capacity(self, tmp_path: Path, low_threshold) -> None:
        # ROUND-4 (engine `fix/ooc-preflight-overreject-recalibration`): the
        # build-floor gate is now advisory, so this 300k-row parent -- large
        # enough to push its floor into the warn band against the 64 MiB
        # `_MIN_BUDGET_BYTES` floor, but no longer a hard refusal -- now
        # renders a WARNING, not INSUFFICIENT, and preflight exits OK by
        # default (only `--fail-on-warning` would make a warning nonzero).
        big_parent, big_child = _parent_child_tables(300_000)
        config_path = _write_config(tmp_path, tables=(big_parent, big_child))
        with mock.patch(
            "decoy_engine.execution.out_of_core._budget.detect_effective_memory_bytes",
            return_value=1024 * 1024,  # 1 MiB ceiling -> a tiny resolved budget
        ):
            result = _run_preflight(config_path)
        assert result.exit_code == EXIT_OK
        assert "capacity:" in result.output
        assert "ADVISORY" in result.output
        assert "INSUFFICIENT" not in result.output

    def test_warned_with_fail_on_warning_exits_nonzero(self, tmp_path: Path, low_threshold) -> None:
        # `--fail-on-warning` governs whether the advisory itself exits
        # nonzero; default execution (the test above) exits EXIT_OK on the
        # exact same job.
        from decoy.cli.preflight import _WARN_EXIT

        big_parent, big_child = _parent_child_tables(300_000)
        config_path = _write_config(tmp_path, tables=(big_parent, big_child))
        with mock.patch(
            "decoy_engine.execution.out_of_core._budget.detect_effective_memory_bytes",
            return_value=1024 * 1024,
        ):
            result = runner.invoke(app, ["preflight", str(config_path), "--fail-on-warning"])
        assert result.exit_code == _WARN_EXIT
        assert result.exit_code != EXIT_OK

    def test_unknown_older_engine_simulated(self, tmp_path: Path, monkeypatch) -> None:
        """T9 groundwork: with the estimator entrypoint absent (simulating an
        engine older than the one that ships it), the capacity section
        degrades to UNKNOWN/"not checked" and exits 0."""
        import decoy_engine.execution as engine_execution

        monkeypatch.delattr(engine_execution, "estimate_job_capacity")
        config_path = _write_config(tmp_path)
        result = _run_preflight(config_path)
        assert result.exit_code == EXIT_OK
        assert "capacity:" in result.output
        assert "not checked" in result.output
        assert "newer engine" in result.output


class TestUnexpectedEstimatorExceptionPropagates:
    """Codex P1-2 (item 2): an unexpected exception from
    `estimate_job_capacity` must PROPAGATE out of `decoy preflight` (R3),
    never degrade to a WARN that lets the command report a false pass.

    Replaces the prior broad `except Exception: acc.add_warn(...)` at
    `_check_capacity` -- no test asserted that swallow-to-WARN behavior
    directly, but this is the behavior the removed `try/except` existed to
    produce, so this test pins the corrected (propagate) contract in its
    place."""

    def test_unexpected_exception_propagates_not_a_warn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import decoy_engine.execution as engine_exec

        def _boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("unexpected estimator defect (test double)")

        monkeypatch.setattr(engine_exec, "estimate_job_capacity", _boom)
        config_path = _write_config(tmp_path)
        with pytest.raises(RuntimeError, match="unexpected estimator defect"):
            runner.invoke(app, ["preflight", str(config_path)], catch_exceptions=False)


class TestJsonEnvelope:
    def test_warned_json_status_is_warn_not_fail(self, tmp_path: Path, low_threshold) -> None:
        # ROUND-4: this 300k-row shape used to be a fail/INSUFFICIENT; the
        # build-floor gate is now advisory, so the JSON envelope reports a
        # "warn" status (both at the top-level `capacity` block and the
        # matching `checks` entry) with a null `code` (no refusal fired),
        # not "fail".
        big_parent, big_child = _parent_child_tables(300_000)
        config_path = _write_config(tmp_path, tables=(big_parent, big_child))
        with mock.patch(
            "decoy_engine.execution.out_of_core._budget.detect_effective_memory_bytes",
            return_value=1024 * 1024,
        ):
            result = _run_preflight(config_path, json_mode=True)
        assert result.exit_code == EXIT_OK
        payload = _json.loads(result.output)
        assert payload["capacity"]["status"] == "warn"
        assert payload["capacity"]["code"] is None
        capacity_checks = [c for c in payload["checks"] if c["name"] == "capacity.out_of_core_fk"]
        assert capacity_checks[0]["status"] == "warn"

    def test_fanin_json_status_is_fail_with_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The fan-in refusal IS constructible through a real FK graph --
        # `_compat.py` only rejects multiple parents resolving the SAME
        # (child_table, child_columns) tuple, not a table with many DISTINCT
        # incoming edges (see `tests/e2e/test_run_preflight_capacity_parity.py::
        # TestRealFanInIsReachable` for a real 67-distinct-parent topology
        # that drives it). This test instead pins the CLI's rendering of a
        # PROPAGATED fan-in ExecutionError from `estimate_job_capacity` (the
        # code path added alongside the engine's round-4 change) against a
        # small, deterministic double of the estimator boundary.
        import decoy_engine.execution as engine_exec
        from decoy_engine import ExecutionError

        def _boom(*_a: Any, **_k: Any) -> Any:
            raise ExecutionError(
                code="out_of_core_fanin_exceeds_budget",
                message="fan-in exceeds budget (test double).",
            )

        monkeypatch.setattr(engine_exec, "estimate_job_capacity", _boom)
        config_path = _write_config(tmp_path)
        result = _run_preflight(config_path, json_mode=True)
        assert result.exit_code == EXIT_CAPACITY
        payload = _json.loads(result.output)
        assert payload["capacity"]["status"] == "fail"
        assert payload["capacity"]["code"] == "out_of_core_fanin_exceeds_budget"
        capacity_checks = [c for c in payload["checks"] if c["name"] == "capacity.out_of_core_fk"]
        assert capacity_checks[0]["code"] == payload["capacity"]["code"]


class TestRelativeSourcePath:
    def test_relative_path_resolves_against_yaml_directory(
        self, tmp_path: Path, low_threshold
    ) -> None:
        # Both the source-file check (Step 4) AND the capacity check resolve
        # relative source paths against the YAML's directory, the same way
        # `decoy run` does (R2). The sources exist under tmp_path, so Step 4
        # passes, capacity resolves the same files and reports OK, and the
        # overall exit is clean -- previously Step 4 resolved against CWD and
        # could spuriously report the relative path missing (Codex re-gate
        # MEDIUM: that asymmetry let a genuinely-missing source pass Step 4
        # while capacity skipped it, exiting 0 on a job `decoy run` would fail).
        config_path = _write_config(tmp_path, relative_paths=True)
        result = _run_preflight(config_path, json_mode=True)
        assert result.exit_code == EXIT_OK
        payload = _json.loads(result.output)
        assert payload["capacity"]["status"] == "pass"
        assert "OK" in payload["capacity"]["message"]
        assert _check_status(payload, "source.parent.exists") == "pass"

    def test_relative_source_missing_in_yaml_dir_fails_even_if_cwd_has_namesake(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression (Codex re-gate MEDIUM, reproduced): a relative source that
        # is MISSING in the YAML directory must FAIL Step 4 and produce a
        # non-zero exit, even when a same-named file exists in the process CWD.
        # Before the fix, Step 4 resolved against CWD (found the namesake ->
        # pass) while the capacity guard resolved against the YAML dir (missing
        # -> skip), so preflight exited 0 on a job the runner would reject.
        yaml_dir = tmp_path / "proj"
        yaml_dir.mkdir()
        cfg = {
            "version": 1,
            "global_settings": {"seed": 7},
            "sources": {"parent": {"type": "file", "path": "parent.parquet", "format": "parquet"}},
            "tables": [{"name": "parent", "columns": [{"name": "id", "strategy": "passthrough"}]}],
            "targets": {
                "parent": {
                    "type": "file",
                    "path": str(tmp_path / "parent.out.parquet"),
                    "format": "parquet",
                }
            },
        }
        config_path = yaml_dir / "pipeline.yaml"
        config_path.write_text(yaml.dump(cfg), encoding="utf-8")
        # A namesake in the CWD that must NOT satisfy the check.
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        pq.write_table(pa.table({"id": pa.array(range(3))}), cwd / "parent.parquet")
        monkeypatch.chdir(cwd)

        result = _run_preflight(config_path, json_mode=True)
        assert result.exit_code != EXIT_OK
        payload = _json.loads(result.output)
        assert _check_status(payload, "source.parent.exists") == "fail"


class TestUnreadableSource:
    @pytest.mark.skipif(
        os.geteuid() == 0, reason="chmod 000 is bypassed by root; unreadability not enforced"
    )
    def test_unreadable_source_does_not_crash_preflight(
        self, tmp_path: Path, low_threshold
    ) -> None:
        # Regression (dennis + Codex re-gate MEDIUM): a source that exists but
        # is unreadable must NOT crash the command. The capacity guard skips it
        # (its `profile_source` read would raise, and the broad catch is gone
        # for R3), so preflight reports Step 4's readability `fail` cleanly and
        # a skipped capacity check, exiting non-zero -- never a traceback.
        config_path = _write_config(tmp_path)
        (tmp_path / "parent.parquet").chmod(0)
        try:
            result = _run_preflight(config_path, json_mode=True)
        finally:
            (tmp_path / "parent.parquet").chmod(0o600)
        assert result.exit_code != EXIT_OK
        payload = _json.loads(result.output)
        assert _check_status(payload, "source.parent.readable") == "fail"
        assert payload["capacity"]["status"] == "pass"
        assert "not checked" in payload["capacity"]["message"]


class TestCorruptSource:
    def test_corrupt_source_reports_capacity_fail_not_a_crash(
        self, tmp_path: Path, low_threshold
    ) -> None:
        # A source that opens fine but is not valid Parquet passes the on-disk
        # accessibility guard and fails when the estimator profiles it. Preflight
        # must render a clean capacity FAIL (non-zero exit), NOT crash on a raw
        # pyarrow traceback with every finding discarded (Codex re-gate MEDIUM).
        config_path = _write_config(tmp_path)
        (tmp_path / "child.parquet").write_bytes(b"NOT_A_PARQUET_FILE_corrupt")
        result = _run_preflight(config_path, json_mode=True)
        # No crash: a raw pyarrow error would abort before the result is emitted
        # and leave a non-SystemExit exception. A clean non-zero exit surfaces as
        # SystemExit, and the JSON result is still produced.
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert result.exit_code != EXIT_OK
        payload = _json.loads(result.output)
        assert _check_status(payload, "capacity.source_unreadable") == "fail"


class TestExecutionNeverDispatched:
    def test_preflight_never_calls_the_execution_entrypoint(
        self, tmp_path: Path, low_threshold
    ) -> None:
        config_path = _write_config(tmp_path)
        with mock.patch(
            "decoy_engine.execution.out_of_core.run_fk_out_of_core",
            side_effect=AssertionError("preflight must never dispatch to the runner"),
        ) as spy:
            result = _run_preflight(config_path)
        assert result.exit_code == EXIT_OK
        spy.assert_not_called()
