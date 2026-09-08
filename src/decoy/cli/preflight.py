"""`decoy preflight` -- local file, source, and schema readiness checks (SP-17).

`preflight` helps catch common problems BEFORE running a pipeline. It performs
local checks that do not require the engine to execute the full pipeline.

What preflight checks (honest framing)
----------------------------------------
1. YAML syntax:        Is the pipeline file a valid YAML document?
2. Schema:             Does the config satisfy the PipelineConfig schema?
3. Config logic:       Profile-free plan-compile checks (same as `decoy validate`).
4. File existence:     Does each declared source file exist on disk?
5. File readability:   Can each source file be opened for reading?
6. Target overwrite:   Does any output target already exist (advisory)?
7. Capacity (v1):      Would the engine's out-of-core-FK memory gate refuse this
                       job? Covers the out-of-core-FK route ONLY -- see the OOM
                       checker v1 note below.

What preflight does NOT check
------------------------------
* Platform preflight conditions (the platform's server-side pre-run checks
  include secrets, RBAC, schedule validity, and network targets -- none of
  those are local CLI checks).
* Data validity or masking quality.
* Engine run-time constraints beyond the out-of-core-FK capacity check above
  (provider capacity, null-bearing integer distributions) -- these require a
  profile pass during the run.
* Vault or secrets accessibility.
* Network connectivity.
* Whether the engine can successfully execute the pipeline.

OOM checker v1 (docs/plans/2026-07-24-oom-checker-cli-v1.md): the capacity
check is an "OOC-FK engine-gate capacity checker", NOT a whole-job OOM
guarantee. `decoy run` fully loads every source into memory BEFORE it calls
the engine, so an ingestion `MemoryError` or OS OOM-kill happens before this
gate ever runs -- this check does not cover that, and does not cover the
generate path. A "capacity: OK" here means only that no hard impossibility was
found. The build-floor estimate is ADVISORY: when the predicted relation-build
floor exceeds its cap, the check reports a WARNING with a recommended size and
the job still proceeds (`--fail-on-warning` turns that warning into a nonzero
exit). The one hard capacity refusal that exits EXIT_CAPACITY is a fan-in
impossibility (more co-live DuckDB joiners than the budget can seat).

Do NOT describe the output of this command as "platform parity." Preflight
here is a local file/source/schema readiness gate. The spec (cli-first-
capability-guide.md lines 99-100) is explicit: frame this as FILE / SOURCE /
SCHEMA checks. Platform preflight is a different product.

The same engine primitives (PipelineConfig.model_validate,
run_config_only_checks) are used for steps 1-3; this keeps both commands
in sync on what "structurally valid" means without literally reusing the
validate command's code path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import typer
import yaml as _yaml

from decoy.cli.exit_codes import EXIT_CAPACITY, EXIT_USAGE
from decoy.ui.output import OutputMode, emit_json, setup_output
from decoy.ui.theme import code, error, hint, success, warn

_PREFLIGHT_EPILOG = """\
Examples:

  decoy preflight pipeline.yaml
    Run local readiness checks: YAML validity, schema, and source file existence.

  decoy preflight pipeline.yaml --local
    Same as above (--local is the explicit form; all checks are local by default).

  decoy preflight pipeline.yaml --json
    Emit a structured JSON result with per-check findings.

  decoy preflight pipeline.yaml --fail-on-warning
    Exit non-zero when advisory warnings fire (e.g. output already exists).

What preflight checks:
  - YAML syntax and schema (same as `decoy validate`)
  - Source file existence and readability
  - Target overwrite risk (advisory warning)
  - Out-of-core-FK memory capacity (v1; exits EXIT_CAPACITY if insufficient --
    see: decoy explain exit-codes)

What preflight does NOT check:
  - Platform server-side conditions (secrets, RBAC, schedules, network targets)
  - Engine run-time constraints beyond out-of-core-FK capacity (row counts,
    provider limits)
  - The ingestion peak `decoy run` pays before the engine's capacity gate runs,
    or the generate path (both out of the v1 capacity check's scope)
  - Data validity or masking quality
  - Vault or secrets accessibility

See also: decoy validate, decoy run, decoy evidence verify, decoy explain exit-codes.
"""

_WARN_EXIT = 2


# ---------------------------------------------------------------------------
# Check accumulator (reused from validate)
# ---------------------------------------------------------------------------


@dataclass
class _CheckResult:
    name: str
    status: str  # "pass" | "warn" | "fail"
    message: str
    location: str | None = None
    # OOM checker v1: the engine's capacity-refusal code (one of
    # out_of_core_insufficient_memory / out_of_core_fanin_exceeds_budget),
    # carried as its own field so a --json caller can assert on it directly
    # instead of parsing `message` text (R9).
    code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "message": self.message,
        }
        if self.location is not None:
            d["location"] = self.location
        if self.code is not None:
            d["code"] = self.code
        return d


@dataclass
class _PreflightAccumulator:
    checks: list[_CheckResult] = field(default_factory=list)
    # OOM checker v1 (T7): an INSUFFICIENT capacity verdict must exit
    # EXIT_CAPACITY, not fold into the generic has_failures -> EXIT_USAGE
    # path -- tracked separately from `checks` status so the two exit
    # reasons can never be confused with each other.
    capacity_insufficient: bool = False

    def add_pass(
        self, name: str, message: str, location: str | None = None, code: str | None = None
    ) -> None:
        self.checks.append(
            _CheckResult(name=name, status="pass", message=message, location=location, code=code)
        )

    def add_warn(
        self, name: str, message: str, location: str | None = None, code: str | None = None
    ) -> None:
        self.checks.append(
            _CheckResult(name=name, status="warn", message=message, location=location, code=code)
        )

    def add_fail(
        self, name: str, message: str, location: str | None = None, code: str | None = None
    ) -> None:
        self.checks.append(
            _CheckResult(name=name, status="fail", message=message, location=location, code=code)
        )

    @property
    def has_failures(self) -> bool:
        return any(c.status == "fail" for c in self.checks)

    @property
    def has_warnings(self) -> bool:
        return any(c.status == "warn" for c in self.checks)

    def to_dicts(self) -> list[dict[str, Any]]:
        return [c.to_dict() for c in self.checks]


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_source_files(raw: dict[str, Any], base_dir: Path, acc: _PreflightAccumulator) -> None:
    """Check that every declared source file exists and is readable.

    File checks only. Parser config, column layout, and format inference are
    NOT checked here (those are engine-level concerns that require a profile
    pass). This check is about the files themselves, not their contents.

    Relative source paths resolve against `base_dir` (the pipeline YAML's
    directory), the SAME way `decoy run` and `estimate_job_capacity` resolve
    them (Codex re-gate MEDIUM). Resolving against CWD instead let a
    same-named file in the caller's working directory mask a source that is
    genuinely missing where the runner would look, so preflight could exit 0
    on a job `decoy run` would fail -- and the capacity guard's
    YAML-dir-resolved skip then hid it a second time.
    """
    sources = raw.get("sources") or {}
    if not isinstance(sources, dict):
        return

    for table_name, src in sources.items():
        if not isinstance(src, dict):
            continue

        src_type = src.get("type") or "file"
        if src_type != "file":
            # Only file sources have a local path to check.
            # Non-file sources (database, API) are not checkable here.
            acc.add_pass(
                name=f"source.{table_name}.type",
                message=f"Source {table_name!r}: non-file type {src_type!r}; skipping local file check.",
                location=f"sources.{table_name}.type",
            )
            continue

        src_path_str = src.get("path")
        if not src_path_str:
            acc.add_fail(
                name=f"source.{table_name}.path",
                message=f"Source {table_name!r}: no 'path' declared.",
                location=f"sources.{table_name}.path",
            )
            continue

        src_path = Path(src_path_str)
        if not src_path.is_absolute():
            src_path = base_dir / src_path
        if not src_path.exists():
            acc.add_fail(
                name=f"source.{table_name}.exists",
                message=f"Source file not found: {src_path_str}",
                location=f"sources.{table_name}.path",
            )
            continue

        if not src_path.is_file():
            acc.add_fail(
                name=f"source.{table_name}.is_file",
                message=f"Source path is not a regular file: {src_path_str}",
                location=f"sources.{table_name}.path",
            )
            continue

        # Readability check (open for reading, read 0 bytes).
        try:
            with src_path.open("rb") as fh:
                fh.read(0)
        except OSError as exc:
            acc.add_fail(
                name=f"source.{table_name}.readable",
                message=f"Source file not readable: {src_path_str}: {exc}",
                location=f"sources.{table_name}.path",
            )
            continue

        acc.add_pass(
            name=f"source.{table_name}.exists",
            message=f"Source file exists and is readable: {src_path_str}",
            location=f"sources.{table_name}.path",
        )


def _check_target_overwrite(raw: dict[str, Any], acc: _PreflightAccumulator) -> None:
    """Advisory: warn when an output target file already exists.

    This is a warning, not a failure. The run succeeds by overwriting,
    but this check surfaces the risk in CI so it can be treated as blocking
    with --fail-on-warning.
    """
    targets = raw.get("targets") or {}
    if not isinstance(targets, dict):
        return

    for table_name, target in targets.items():
        if not isinstance(target, dict):
            continue
        if target.get("type") not in ("file", None):
            continue
        out_path_str = target.get("path")
        if not out_path_str:
            continue
        out_path = Path(out_path_str)
        if out_path.exists():
            acc.add_warn(
                name=f"target.{table_name}.would_overwrite",
                message=f"Output target already exists and would be overwritten: {out_path_str}",
                location=f"targets.{table_name}.path",
            )
        else:
            # Check that the parent directory exists or can be created.
            parent = out_path.parent
            if not parent.exists():
                acc.add_warn(
                    name=f"target.{table_name}.parent_missing",
                    message=f"Output target directory does not exist: {parent!s}",
                    location=f"targets.{table_name}.path",
                )


# ---------------------------------------------------------------------------
# Capacity check (OOM checker v1, docs/plans/2026-07-24-oom-checker-cli-v1.md)
# ---------------------------------------------------------------------------

_GIB = 1024**3


def _format_gib(byte_count: int | None) -> str:
    return f"~{byte_count / _GIB:.1f} GB" if byte_count else "an unknown amount"


def _file_sources_missing_on_disk(raw: dict[str, Any], base_dir: Path) -> bool:
    """True if any declared FILE-type source does not resolve to a readable
    regular file, resolved the SAME way `estimate_job_capacity`,
    `_check_source_files`, and `decoy run` resolve them (relative to
    `base_dir`, the pipeline YAML's directory -- R2). All three now agree on
    resolution, so a source missing where the runner would look is reported
    by Step 4 as a `fail` AND skipped here, never silently passed.

    Root-cause guard for a real regression: without it, a job whose source
    is already known missing (Step 4's `_check_source_files` already
    reported a `fail` for it) would still reach `estimate_job_capacity`,
    which raises on an unreadable source (its own documented R3 contract,
    "an unreadable source" propagates) -- and since that exception is no
    longer swallowed here (R3, Codex P1-2), it would crash the WHOLE
    `preflight` command before `_emit_preflight_result` ever runs, losing
    every finding accumulated so far, including the one that already
    explained the problem. Skipping the capacity estimate for a source we
    already know cannot be read is not swallowing a defect -- it is
    declining to run a check whose precondition is already known false,
    exactly like the sibling non-file-source skip below.
    """
    sources = raw.get("sources") or {}
    if not isinstance(sources, dict):
        return False
    for src in sources.values():
        if not isinstance(src, dict) or (src.get("type") or "file") != "file":
            continue
        path_str = src.get("path")
        if not path_str:
            return True
        path = Path(path_str)
        if not path.is_absolute():
            path = base_dir / path
        if not path.is_file():
            return True
        # Present but UN-OPENABLE counts too (Codex re-gate MEDIUM): a
        # permission-denied file opens-then-fails, and `estimate_job_capacity`
        # -> `profile_source` opens the source, so with the broad catch removed
        # (R3) it would crash the command. Skip the estimate; Step 4's
        # readability check already reported the `fail`. This tests filesystem
        # ACCESSIBILITY only (open + zero-byte read) -- a file that opens but is
        # truncated/corrupt/wrong-format is NOT caught here (parsing would be
        # required); that case is handled downstream by the estimator's typed
        # `capacity_source_unprofilable` error, rendered as a capacity fail.
        try:
            with path.open("rb") as fh:
                fh.read(0)
        except OSError:
            return True
    return False


def _check_capacity(raw: dict[str, Any], config_path: Path, acc: _PreflightAccumulator) -> None:
    """Predict the out-of-core-FK memory-capacity gate before a run starts.

    R4 honest framing: this covers ONE gate -- the out-of-core-FK route's
    resident-memory floor. `decoy run` fully loads every source into memory
    BEFORE it ever calls the engine, so an ingestion `MemoryError` or OS
    OOM-kill happens before this gate runs; a "capacity: OK" here says
    nothing about that. See the module docstring's "OOM checker v1" note.

    R5 capability-detect: an engine older than the one that ships
    `estimate_job_capacity` degrades to "not checked" rather than an
    ImportError -- the CLI's floor stays >=0.5.0 either way.

    R3 (Codex P1-2): an UNEXPECTED exception from `estimate_job_capacity`
    (a genuine engine defect -- a compile bug, ...) is NOT caught here.
    `estimate_job_capacity` itself only raises for a real defect (its own R3
    contract) or returns a typed verdict, including `UNKNOWN` for EXPECTED
    indeterminacy (an undetectable memory ceiling, an unpriceable CSV row
    count) -- so an exception reaching this call site is never "capacity
    merely unknown", and folding it into a WARN here would make `decoy
    preflight` report a false pass on a real defect. Only the THREE guarded
    early-returns below (capability-detect, non-file sources, a source
    already known missing/unreadable from disk) degrade this section
    without running the estimator at all; once it runs, its exceptions
    propagate and its `UNKNOWN` verdict renders as "not checked". The third
    guard exists because a missing source is ALSO one of `estimate_job_
    capacity`'s own documented propagating cases ("an unreadable source") --
    without it, a job Step 4 (`_check_source_files`) already flagged as
    missing would reach the estimator a second time and crash the whole
    command before any finding (including Step 4's own) is ever printed.

    Scope + posture: this section reads FILE-source metadata (parquet footer /
    a bounded local sample) to size the job. To keep preflight local and
    no-network, it runs only when every source is a file; if any source is
    non-file (database/API), capacity is "not checked" here rather than opening
    a remote connection during a "local" preflight.
    """
    import decoy_engine.execution as _engine_execution

    if not hasattr(_engine_execution, "estimate_job_capacity"):
        acc.add_pass(
            name="capacity.out_of_core_fk",
            message="not checked -- capacity check needs a newer engine.",
        )
        return

    # Preserve preflight's local, no-network posture: the engine estimate reads
    # each source, which for a non-file source means opening a DB/remote
    # connection. A "local" preflight must not do that, so skip capacity when
    # any source is non-file (matches _check_source_files' file-only scope).
    sources = raw.get("sources") or {}
    if isinstance(sources, dict) and any(
        isinstance(s, dict) and (s.get("type") or "file") != "file" for s in sources.values()
    ):
        acc.add_pass(
            name="capacity.out_of_core_fk",
            message="not checked -- capacity is estimated only for file sources in local preflight.",
        )
        return

    # A source already known missing/unreadable (Step 4 already reported this
    # as a `fail`) cannot be estimated -- see `_file_sources_missing_on_disk`'s
    # docstring for why this guard exists (a real regression it fixes).
    if _file_sources_missing_on_disk(raw, config_path.parent):
        acc.add_pass(
            name="capacity.out_of_core_fk",
            message="not checked -- one or more declared source files are missing or "
            "unreadable; see the source-file findings above.",
        )
        return

    from decoy_engine import CapacityVerdict, ExecutionError, PipelineConfig

    # The ONE narrow catch here (R3): a source that is present but corrupt /
    # not-the-declared-format passes the on-disk guard above (which only tests
    # filesystem accessibility, not parseability) and fails when the estimator
    # profiles it -- `estimate_job_capacity` raises the typed
    # `capacity_source_unprofilable` for exactly that. Render it as a FAIL (the
    # profile read is the first check that actually parses the source, so this
    # is where a corrupt file legitimately surfaces) rather than crashing on a
    # raw traceback. Any OTHER exception -- an unexpected estimator defect --
    # still propagates, never degraded into a WARN or a false pass (Codex
    # P1-2 / re-gate MEDIUM).
    config_dump = PipelineConfig.model_validate(raw).model_dump()
    try:
        estimate = _engine_execution.estimate_job_capacity(config_dump, config_path.parent)
    except ExecutionError as exc:
        if exc.code == "out_of_core_fanin_exceeds_budget":
            # Round-4 (engine): the build-floor gate is now advisory, so
            # `estimate_job_capacity` no longer returns INSUFFICIENT for
            # that case -- but a fan-in impossibility discovered while
            # resolving the memory budget still PROPAGATES as this
            # ExecutionError instead of coming back as a verdict. Render it
            # the same way the INSUFFICIENT verdict branch below does (a
            # FAIL that trips EXIT_CAPACITY), so a fan-in job still refuses
            # cleanly instead of crashing preflight on a raw traceback.
            acc.capacity_insufficient = True
            acc.add_fail(
                name="capacity.out_of_core_fk",
                message=f"INSUFFICIENT -- {exc.message}",
                code=exc.code,
            )
            return
        if exc.code != "capacity_source_unprofilable":
            raise
        acc.add_fail(
            name="capacity.source_unreadable",
            message=f"Cannot estimate capacity -- {exc.message}",
            code=exc.code,
        )
        return

    needed = _format_gib(estimate.needed_bytes)
    available = _format_gib(estimate.available_bytes)

    if estimate.verdict is CapacityVerdict.FIT and estimate.warned:
        # Round-4: FIT no longer means "clears the build-floor budget" -- it
        # means "no hard impossibility detected". `warned=True` marks an
        # adverse build-floor prediction (relation-build only; excludes
        # resident inputs, accumulated outputs, and ingestion peak); render
        # it as a WARNING, not a green PASS, so `--fail-on-warning` can act
        # on it, but the job is not refused either way.
        acc.add_warn(
            name="capacity.out_of_core_fk",
            message=(
                "ADVISORY -- OOC-FK estimated relation-build floor exceeds the build "
                f"cap it would receive; recommend {needed} of memory (budget {available}). "
                "The job is not refused; this is a recommendation, not a hard limit."
            ),
        )
    elif estimate.verdict is CapacityVerdict.FIT:
        acc.add_pass(
            name="capacity.out_of_core_fk",
            message=(
                "OK -- OOC-FK estimated resident floor is within budget (does not "
                f"include ingestion peak). Needs {needed}, budget {available}."
            ),
        )
    elif estimate.verdict is CapacityVerdict.INSUFFICIENT:
        acc.capacity_insufficient = True
        acc.add_fail(
            name="capacity.out_of_core_fk",
            message=(
                f"INSUFFICIENT -- needs {needed}, budget {available}; use a larger "
                "tier or reduce the job. This estimate is conservative: a real run "
                "may still admit the job via full-frame recovery."
            ),
            code=estimate.code,
        )
    elif estimate.verdict is CapacityVerdict.UNKNOWN:
        # An uncertain route (a byte-estimate-promoted OOC job) forces UNKNOWN,
        # but a build-floor advisory can still ride along on the estimate. Surface
        # it as a warning so the recommendation is not lost and --fail-on-warning
        # can act on it, exactly as it does on the confirmed FIT+warned path.
        if estimate.warned:
            acc.add_warn(
                name="capacity.out_of_core_fk",
                message=f"ADVISORY (route unconfirmed) -- {estimate.message}",
            )
        else:
            acc.add_pass(
                name="capacity.out_of_core_fk", message=f"not checked -- {estimate.message}"
            )
    else:  # NOT_APPLICABLE
        acc.add_pass(
            name="capacity.out_of_core_fk", message=f"not applicable -- {estimate.message}"
        )


# ---------------------------------------------------------------------------
# Main command
# ---------------------------------------------------------------------------


def preflight(
    config: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        help="Path to the YAML pipeline config to check.",
    ),
    local: bool = typer.Option(
        False,
        "--local",
        help=(
            "Explicit local mode (all checks are local by default; "
            "this flag makes the intent explicit in scripts)."
        ),
    ),
    json_: bool = typer.Option(
        False,
        "--json",
        help="Emit a structured JSON result on stdout.",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Suppress stdout. Errors still go to stderr; exit code carries the result.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable debug-level CLI logs on stderr.",
    ),
    fail_on_warning: bool = typer.Option(
        False,
        "--fail-on-warning",
        help="Exit non-zero when any advisory warning fires.",
    ),
) -> None:
    """Local pre-run readiness checks for a pipeline config.

    Checks file existence, file readability, YAML syntax, schema validity,
    and (v1) the engine's out-of-core-FK memory feasibility. The build-floor
    estimate is advisory: an over-budget prediction reports a WARNING with a
    recommended size, not a refusal (`--fail-on-warning` makes it exit
    nonzero). Only a fan-in impossibility exits EXIT_CAPACITY (see `decoy
    explain exit-codes`), distinct from a config problem (EXIT_USAGE). Reports
    findings as pass/warn/fail with structured output available via --json.

    This is a LOCAL check only. It does NOT check platform server-side
    conditions, most engine run-time constraints, data quality, vault
    access, secrets availability, or network connectivity. The capacity
    check covers the out-of-core-FK route only -- it does not cover the
    ingestion peak `decoy run` pays before the engine's gate runs, or the
    generate path. Use `decoy validate` for pure schema-only checks; use
    this command when you want to confirm source files are present and the
    job would clear the memory gate before starting a run.
    """
    state = setup_output(json_, quiet, verbose)
    config_str = str(config)
    acc = _PreflightAccumulator()

    from decoy_engine import (
        ConfigError,
        PipelineConfig,
        PipelineValidationError,
        run_config_only_checks,
    )
    from decoy_engine.plan import PlanCompileError
    from pydantic import ValidationError

    # -- Step 1: YAML parse ---------------------------------------------------
    try:
        raw = _yaml.safe_load(config.read_text(encoding="utf-8"))
    except _yaml.YAMLError as exc:
        acc.add_fail(name="yaml.parse_error", message=f"YAML parse error: {exc}")
        _emit_preflight_result(state, acc, config_str, fail_on_warning)
        raise typer.Exit(code=EXIT_USAGE)

    if raw is None:
        acc.add_fail(name="yaml.empty", message="Pipeline YAML is empty.")
        _emit_preflight_result(state, acc, config_str, fail_on_warning)
        raise typer.Exit(code=EXIT_USAGE)

    if not isinstance(raw, dict):
        acc.add_fail(
            name="yaml.not_mapping",
            message=f"Pipeline YAML must be a YAML mapping (object), not {type(raw).__name__}.",
        )
        _emit_preflight_result(state, acc, config_str, fail_on_warning)
        raise typer.Exit(code=EXIT_USAGE)

    # YAML parse passed
    acc.add_pass(name="yaml.syntax", message="YAML syntax is valid.")

    # -- Step 2: Schema validation --------------------------------------------
    try:
        PipelineConfig.model_validate(raw)
        acc.add_pass(name="schema.valid", message="Pipeline schema is valid.")
    except ValidationError as exc:
        for e in exc.errors():
            loc_parts = [str(p) for p in e.get("loc", ())]
            location = ".".join(loc_parts) if loc_parts else None
            acc.add_fail(
                name="schema.field",
                message=e.get("msg", str(e)),
                location=location,
            )
        _emit_preflight_result(state, acc, config_str, fail_on_warning)
        raise typer.Exit(code=EXIT_USAGE)
    except (PipelineValidationError, ConfigError) as exc:
        acc.add_fail(name="schema.validation_error", message=str(exc))
        _emit_preflight_result(state, acc, config_str, fail_on_warning)
        raise typer.Exit(code=EXIT_USAGE)

    # -- Step 3: Profile-free plan-compile checks ----------------------------
    try:
        run_config_only_checks(raw)
        acc.add_pass(name="config.plan_checks", message="Profile-free plan-compile checks passed.")
    except PlanCompileError as exc:
        acc.add_fail(name=f"config.{exc.code}", message=exc.message)
        _emit_preflight_result(state, acc, config_str, fail_on_warning)
        raise typer.Exit(code=EXIT_USAGE)

    # -- Step 4: Source file checks ------------------------------------------
    _check_source_files(raw, config.parent, acc)

    # -- Step 5: Target overwrite advisory -----------------------------------
    _check_target_overwrite(raw, acc)

    # -- Step 6: Capacity check (OOM checker v1) -----------------------------
    _check_capacity(raw, config, acc)

    # -- Emit and exit --------------------------------------------------------
    _emit_preflight_result(state, acc, config_str, fail_on_warning)

    # T7: INSUFFICIENT capacity exits EXIT_CAPACITY, checked BEFORE the
    # generic has_failures path so it is never masked as a plain EXIT_USAGE
    # config problem -- a distinct, machine-detectable "needs more memory"
    # result up front, before a run starts.
    if acc.capacity_insufficient:
        raise typer.Exit(code=EXIT_CAPACITY)
    if acc.has_failures:
        raise typer.Exit(code=EXIT_USAGE)
    if fail_on_warning and acc.has_warnings:
        raise typer.Exit(code=_WARN_EXIT)


def _emit_preflight_result(
    state: Any,
    acc: _PreflightAccumulator,
    config_str: str,
    fail_on_warning: bool,
) -> None:
    """Render the accumulated preflight result."""
    capacity_checks = [c for c in acc.checks if c.name == "capacity.out_of_core_fk"]

    if state.mode is OutputMode.json:
        status = "ok" if not acc.has_failures else "fail"
        if status == "ok" and fail_on_warning and acc.has_warnings:
            status = "warn"
        payload: dict[str, Any] = {
            "command": "preflight",
            "status": status,
            "config": config_str,
            "checks": acc.to_dicts(),
            "fail_count": sum(1 for c in acc.checks if c.status == "fail"),
            "warn_count": sum(1 for c in acc.checks if c.status == "warn"),
            "pass_count": sum(1 for c in acc.checks if c.status == "pass"),
        }
        # A structured top-level `capacity` block, in addition to its entry
        # in `checks` -- R9: assertable without parsing display text.
        if capacity_checks:
            cap = capacity_checks[0]
            payload["capacity"] = {
                "status": cap.status,
                "message": cap.message,
                "code": cap.code,
            }
        emit_json(state, payload)
        return

    if state.mode is OutputMode.quiet:
        return

    # Human-readable: print warnings then failures, then overall result.
    for chk in acc.checks:
        if chk.name == "capacity.out_of_core_fk":
            continue  # rendered separately below, at every status (not just warn/fail)
        if chk.status == "warn":
            loc_hint = f" ({chk.location})" if chk.location else ""
            state.err_console.print(warn("warning:"), chk.message + loc_hint)
        elif chk.status == "fail":
            loc_hint = f" ({chk.location})" if chk.location else ""
            state.err_console.print(error("fail:"), chk.message + loc_hint)

    # The capacity line always prints, regardless of pass/warn/fail: OK, not
    # checked, and not applicable are all informative outcomes an operator
    # should see, not just the failure case.
    for cap in capacity_checks:
        if cap.status == "fail":
            label = error("capacity:")
        elif cap.status == "warn":
            label = warn("capacity:")
        else:
            label = hint("capacity:")
        state.err_console.print(label, cap.message)

    if acc.has_failures:
        state.err_console.print(
            error("FAIL"),
            code(config_str),
            hint("- fix the errors above, then rerun."),
        )
        return

    state.console.print(success("OK"), code(config_str))

    if acc.has_warnings and fail_on_warning:
        state.err_console.print(
            warn("warning:"),
            "warnings present and --fail-on-warning is set.",
        )


PREFLIGHT_EPILOG = _PREFLIGHT_EPILOG
