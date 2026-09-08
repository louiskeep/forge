"""T8 (docs/plans/2026-07-24-oom-checker-cli-v1.md, "Revised acceptance
tests"): one job, run through BOTH `decoy preflight` and `decoy run`, must
agree -- preflight INSUFFICIENT <=> run raises + exits EXIT_CAPACITY. Real
derivation on both sides (real files, real `evaluate_capacity`), not a
mocked verdict standing in for either command.

ROUND-4 (engine `fix/ooc-preflight-overreject-recalibration`): the
build-floor gate is now advisory, so it can no longer produce a mutual
INSUFFICIENT/EXIT_CAPACITY refusal at all -- the former 300k-row build-floor
case now yields a mutual WARNING instead (`test_build_floor_case_now_warns_
on_both_sides`). Fan-in is the only refusal left, and it IS constructible
through a real FK graph: `_compat.py` only rejects multiple parents for the
SAME (child_table, child_columns) tuple, not a table with many DISTINCT
incoming edges. `TestRealFanInIsReachable` builds a 67-distinct-parent
topology (each parent on its own child column) so `child`'s incoming-edge
count reaches 67, and the resident-path phase sizer opens that table's
joiners+build at `incoming_edges + 1 = 68` co-live DuckDB instances -- more
than the 64 MiB `_MIN_BUDGET_BYTES` floor can seat even a 1 MB `memory_limit`
apiece, so both commands hit the real, unmocked `out_of_core_fanin_exceeds_
budget` raise. `test_fanin_agrees_preflight_and_run_both_refuse` below still
mocks the engine boundary directly -- kept as a smaller, deterministic pin of
the CLI's own rendering/exit-code contract for that code, now that the real
topology test above it proves the condition is genuinely reachable.

Both commands need the SAME lowered out-of-core size threshold (neither
exposes a CLI flag for it): `low_threshold` patches `decoy_engine.execution.
capacity.decide_execution_route` for preflight and `decoy_engine.run_pipeline`
for run, both with the identical override, so the two commands see the
identical routing knob.
"""

from __future__ import annotations

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


def _hash_col(name: str, namespace: str) -> dict[str, Any]:
    return {"name": name, "strategy": "hash", "namespace": namespace}


def _parent_child_tables(n: int) -> tuple[pa.Table, pa.Table]:
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


def _write_config(tmp_path: Path, parent: pa.Table, child: pa.Table) -> Path:
    pq.write_table(parent, tmp_path / "parent.parquet")
    pq.write_table(child, tmp_path / "child.parquet")
    cfg = {
        "version": 1,
        "global_settings": {"seed": 7},
        "sources": {
            "parent": {
                "type": "file",
                "path": str(tmp_path / "parent.parquet"),
                "format": "parquet",
            },
            "child": {"type": "file", "path": str(tmp_path / "child.parquet"), "format": "parquet"},
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


@pytest.fixture()
def low_threshold_both_commands(monkeypatch: pytest.MonkeyPatch):
    """The SAME lowered out-of-core threshold for both `decoy preflight`
    (patches the estimator's own routing call) and `decoy run` (patches
    `decoy_engine.run_pipeline`, the seam `run.py` imports fresh per
    invocation) -- one fixture, so a parity test can never accidentally
    compare the two commands under different routing knobs."""
    import decoy_engine
    import decoy_engine.execution.capacity as capacity_mod

    real_decide = capacity_mod.decide_execution_route
    real_run_pipeline = decoy_engine.run_pipeline

    def _patched_decide(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("out_of_core_threshold_rows", 10)
        kwargs.setdefault("full_frame_reject_rows", 10)
        return real_decide(*args, **kwargs)

    def _patched_run_pipeline(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("out_of_core_threshold_rows", 10)
        kwargs.setdefault("full_frame_reject_rows", 10)
        return real_run_pipeline(*args, **kwargs)

    monkeypatch.setattr(capacity_mod, "decide_execution_route", _patched_decide)
    monkeypatch.setattr(decoy_engine, "run_pipeline", _patched_run_pipeline)


class TestParity:
    def test_fit_agrees_preflight_ok_run_succeeds(
        self, tmp_path: Path, low_threshold_both_commands
    ) -> None:
        parent, child = _parent_child_tables(40)
        config_path = _write_config(tmp_path, parent, child)

        preflight_result = runner.invoke(app, ["preflight", str(config_path)])
        assert preflight_result.exit_code == EXIT_OK
        assert "OK" in preflight_result.output

        run_result = runner.invoke(app, ["run", str(config_path)])
        assert run_result.exit_code == EXIT_OK

    def test_build_floor_case_now_warns_on_both_sides(
        self, tmp_path: Path, low_threshold_both_commands
    ) -> None:
        # ROUND-4: this 300k-row shape used to be a mutual hard refusal
        # (`out_of_core_insufficient_memory`) at a 1 MiB detected ceiling
        # (floored to the 64 MiB `_MIN_BUDGET_BYTES` minimum). It is now
        # mutual-advisory -- but AT that exact 64 MiB cap, the floor/cap
        # margin is razor-thin (~3 MB), and a REAL run can still genuinely
        # OOM inside DuckDB there (the advisory recommends more memory; it
        # does not guarantee the job fits at a cap this tight -- see the
        # engine plan's own risk section). A slightly larger detected
        # ceiling (2 GiB + 100 MiB, giving a ~100 MB cap against this
        # parent's ~58 MB floor) keeps the SAME warn-band outcome
        # (floor >= 0.6 * cap) with real headroom, so `run` actually
        # completes rather than racing DuckDB's own allocator at the edge.
        parent, child = _parent_child_tables(300_000)
        config_path = _write_config(tmp_path, parent, child)
        ceiling_bytes = 2 * 1024 * 1024 * 1024 + 100 * 1024 * 1024

        with mock.patch(
            "decoy_engine.execution.out_of_core._budget.detect_effective_memory_bytes",
            return_value=ceiling_bytes,
        ):
            preflight_result = runner.invoke(app, ["preflight", str(config_path)])
            run_result = runner.invoke(app, ["run", str(config_path)])

        assert preflight_result.exit_code == EXIT_OK
        assert "ADVISORY" in preflight_result.output
        assert "OK" in preflight_result.output  # not a fail: the overall command still passes
        assert "INSUFFICIENT" not in preflight_result.output

        assert run_result.exit_code == EXIT_OK

    def test_fanin_agrees_preflight_and_run_both_refuse(self, tmp_path: Path) -> None:
        # `TestRealFanInIsReachable` below proves this condition is reachable
        # through a real 67-distinct-parent FK graph; this test keeps a
        # simpler, deterministic double of the engine boundary so the CLI's
        # own rendering/exit-code parity for a fan-in refusal has a
        # minimal-fixture pin too, independent of that larger topology.
        import decoy_engine
        import decoy_engine.execution as engine_exec
        from decoy_engine import ExecutionError

        parent, child = _parent_child_tables(40)
        config_path = _write_config(tmp_path, parent, child)

        def _boom_estimate(*_a: Any, **_k: Any) -> Any:
            raise ExecutionError(
                code="out_of_core_fanin_exceeds_budget",
                message="fan-in exceeds budget (test double).",
            )

        def _boom_run(*_a: Any, **_k: Any) -> Any:
            raise ExecutionError(
                code="out_of_core_fanin_exceeds_budget",
                message="fan-in exceeds budget (test double).",
            )

        with (
            mock.patch.object(engine_exec, "estimate_job_capacity", _boom_estimate),
            mock.patch.object(decoy_engine, "run_pipeline", _boom_run),
        ):
            preflight_result = runner.invoke(app, ["preflight", str(config_path)])
            run_result = runner.invoke(app, ["run", str(config_path)])

        assert preflight_result.exit_code == EXIT_CAPACITY
        assert "capacity:" in preflight_result.output

        assert run_result.exit_code == EXIT_CAPACITY
        assert "capacity:" in run_result.output


# A table's incoming-edge count on the resident path opens `incoming + 1`
# co-live DuckDB instances (joiners plus that table's own build); raising
# `out_of_core_fanin_exceeds_budget` needs `(incoming + 1) * 1_000_000 >
# _MIN_BUDGET_BYTES` (67_108_864), i.e. incoming >= 67 -- the boundary the
# engine's own `_per_instance_mib` docstring pins ("(64 MiB, 67 live) admits;
# (64 MiB, 68 live) raises").
_FANIN_INCOMING_EDGES = 67
# Exceeds `low_threshold_both_commands`' out_of_core_threshold_rows (10) so
# these tiny tables still route out_of_core; fan-in is a graph-structural
# property, not a size one, so this stays small purely to keep the test fast.
_FANIN_ROWS = 40


def _fanin_config(tmp_path: Path, n_parents: int = _FANIN_INCOMING_EDGES, rows: int = _FANIN_ROWS) -> Path:
    """A schema with `n_parents` distinct parent tables, each contributing
    exactly ONE incoming FK edge into a single shared `child` table on its
    OWN child column -- distinct (child_table, child_columns) tuples per
    edge, so `_compat.py`'s multi-parent-same-child-column rejection never
    fires, while `child`'s total incoming-edge count (what
    `_incoming_edge_counts` sums) still reaches `n_parents`."""
    sources: dict[str, Any] = {}
    tables: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []

    child_cols: dict[str, pa.Array] = {
        "cid": pa.array([f"c{j}" for j in range(rows)], type=pa.string())
    }
    child_columns_cfg: list[dict[str, Any]] = [_hash_col("cid", "cns")]

    for i in range(n_parents):
        pname = f"parent_{i}"
        ns = f"ns{i}"
        parent_ids = pa.array([f"p{i}_{j}" for j in range(rows)], type=pa.string())
        pq.write_table(pa.table({"id": parent_ids}), tmp_path / f"{pname}.parquet")
        sources[pname] = {
            "type": "file",
            "path": str(tmp_path / f"{pname}.parquet"),
            "format": "parquet",
        }
        tables.append({"name": pname, "columns": [_hash_col("id", ns)]})
        fk_col = f"fk_{i}"
        child_cols[fk_col] = parent_ids
        child_columns_cfg.append(_hash_col(fk_col, ns))
        relationships.append(
            {
                "parent": {"table": pname, "columns": ["id"]},
                "children": [{"table": "child", "columns": [fk_col]}],
                "orphan_policy": "preserve",
                "namespace": ns,
            }
        )

    pq.write_table(pa.table(child_cols), tmp_path / "child.parquet")
    sources["child"] = {"type": "file", "path": str(tmp_path / "child.parquet"), "format": "parquet"}
    tables.append({"name": "child", "columns": child_columns_cfg})

    cfg = {
        "version": 1,
        "global_settings": {"seed": 7},
        "sources": sources,
        "targets": {
            "child": {
                "type": "file",
                "path": str(tmp_path / "child.out.parquet"),
                "format": "parquet",
            }
        },
        "tables": tables,
        "relationships": relationships,
    }
    p = tmp_path / "pipeline.yaml"
    p.write_text(yaml.dump(cfg), encoding="utf-8")
    return p


class TestRealFanInIsReachable:
    """No mocking of the engine's capacity boundary: a real 67-distinct-
    parent FK graph drives `child`'s incoming-edge count to 67, so the
    resident-path phase sizer (`resolve_phase_memory_limits` /
    `evaluate_capacity`'s own fan-in loop) opens 68 co-live DuckDB instances
    for it -- more than the 64 MiB `_MIN_BUDGET_BYTES` floor can seat even a
    1 MB `memory_limit` apiece (68 * 1_000_000 > 67_108_864), so both
    `decoy preflight` and `decoy run` hit the real `out_of_core_fanin_
    exceeds_budget` raise, proving the plan's acceptance case end to end."""

    def test_fanin_real_topology_exits_capacity_on_both_commands(
        self, tmp_path: Path, low_threshold_both_commands
    ) -> None:
        config_path = _fanin_config(tmp_path)

        with mock.patch(
            "decoy_engine.execution.out_of_core._budget.detect_effective_memory_bytes",
            return_value=1024 * 1024,  # small ceiling -> resolved budget floors at 64 MiB
        ):
            preflight_result = runner.invoke(app, ["preflight", str(config_path), "--json"])
            run_result = runner.invoke(app, ["run", str(config_path)])

        import json as _json

        payload = _json.loads(preflight_result.output)
        assert preflight_result.exit_code == EXIT_CAPACITY
        assert payload["capacity"]["status"] == "fail"
        assert payload["capacity"]["code"] == "out_of_core_fanin_exceeds_budget"

        assert run_result.exit_code == EXIT_CAPACITY
        assert "capacity:" in run_result.output
