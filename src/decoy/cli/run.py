"""`decoy run` -- execute a masking or synthetic-generation pipeline.

CLI.1 commit 3 (2026-06-01): rewired against the V2 engine spine.
Non-chunked runs (the default) go through `run_pipeline`, the engine's
unified entry point that profiles, compiles, and executes mask + generate
tables in one call, using the engine's internal `PandasExecutionAdapter`.
`--chunked` is a separate streaming path (`run_mask_pipeline_chunked`),
mask-only, which still supports substrate selection via
`select_execution_adapter`. `graph` and `convert` are V1-only and have
no engine; the choke-point validator rejects them with a typed error
before this module sees them.
"""

import binascii
import json as _json
import os
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

import typer

from decoy import __version__ as _cli_version
from decoy.cli.exit_codes import EXIT_CAPACITY, EXIT_RUNTIME, EXIT_USAGE
from decoy.ui.card import render_card
from decoy.ui.output import OutputMode, emit_json, setup_output
from decoy.ui.progress import spinner
from decoy.ui.theme import error, hint, warn

# OOM checker v1: the engine's out-of-core-FK memory gate raises `ExecutionError`
# with exactly one of these two codes when it refuses a job for capacity, never
# any other reason -- matched narrowly so a routing/compat/orphan-FK
# `ExecutionError` (any other code) keeps its existing EXIT_RUNTIME
# classification below. See docs/plans/2026-07-24-oom-checker-cli-v1.md R8.
# `out_of_core_insufficient_memory` is now LEGACY-ENGINE compat (round-4, engine
# side): a current engine's build-floor gate is advisory and never raises it, so
# this code only ever arrives from an older engine still running the pre-round-4
# hard-fail gate; kept recognized here rather than dropped so the CLI stays
# correct against that older engine too.
_CAPACITY_CODES = frozenset({"out_of_core_insufficient_memory", "out_of_core_fanin_exceeds_budget"})


class Mode(str, Enum):
    mask = "mask"
    generate = "generate"


class NotifyOn(str, Enum):
    success = "success"
    failure = "failure"
    always = "always"


_RUN_EPILOG = """\
Examples:

  decoy run pipeline.yaml
    Run with default mode (mask).

  decoy run pipeline.yaml --json
    Suppress chrome and emit a structured result for scripting.

  decoy run pipeline.yaml --chunked --chunk-size 100000
    Stream a large source through the engine instead of loading it whole.
    (See: decoy explain chunked.)

  decoy run pipeline.yaml --vault vault.bin
    Write an encrypted token vault for columns marked `vault: true`, so
    they can be recovered later with `decoy unmask`. (See: decoy explain vault.)

  decoy run pipeline.yaml --chunked --substrate polars
    Stream with polars instead of the chunked-path pandas default.
    (--substrate only affects --chunked runs; plain runs always use pandas.
    See: decoy explain substrate.)

  decoy run pipeline.yaml --notify webhook:https://hooks.example.com/x
    Notify a webhook after the run reaches its terminal state. Repeatable;
    kind in webhook/slack/email. Best-effort: a channel failure never
    changes the run's exit code. Webhook signing key from
    DECOY_NOTIFY_WEBHOOK_SECRET; SMTP from DECOY_NOTIFY_SMTP_HOST/_PORT/
    _USER/_PASS/_FROM. Nothing is persisted to .decoy/workspace.json.

  decoy run pipeline.yaml --notify slack:https://hooks.slack.com/services/x --notify-on failure
    Only notify on a failed run.

See also: decoy validate config, decoy validate distribution, decoy explain chunked, decoy explain vault.
"""


class _VaultUsageError(Exception):
    """--vault on a config that cannot vault; user error (exits EXIT_USAGE)."""


class _ChunkedGenerateError(Exception):
    """--chunked with a generate-table config; user error (exits EXIT_USAGE)."""


class _MaskSecretUsageError(Exception):
    """--mask-secret AND YAML mask_secret_ref both set; user error (exits EXIT_USAGE)."""


class _ConfigValidationError(Exception):
    """The pipeline YAML failed PipelineConfig schema validation (unknown key
    under extra="forbid", wrong-typed / missing field). A raw Pydantic
    ValidationError caught NARROWLY at the model_validate call site and
    re-raised as this typed error, so the run-level handler classifies it
    EXIT_USAGE without also catching internal engine ValidationErrors raised
    deeper in the pipeline (which are engine defects -> EXIT_RUNTIME)."""


# DE-02: the ONE place that decides whether a mask_secret_ref value is
# well-formed. A valid ref is a non-empty `env:NAME` / `file:/PATH` string; a
# non-str (list/int/dict), empty string, or wrong-prefix string is invalid.
# Shared by the raw pre-check (before Pydantic) and the merged effective-ref
# guard so the two can never diverge. Callers ALWAYS raise a redacted error --
# they never echo the value, which may be a raw secret.
def _is_valid_mask_secret_ref(value: object) -> bool:
    return isinstance(value, str) and value.startswith(("env:", "file:"))


def _load_raw_config(config_path: Path) -> dict | None:
    """Single defensive YAML parse for the pre-flight helpers.

    Audit L3 (2026-06-12): the config was read + parsed up to 4x per
    run (_detect_mode, the spinner body, the follow-up hint, and a
    since-removed key_label reader) -- a perf cliff on large or
    network-mounted configs. Helpers now share one parse; the spinner
    body still re-raises real parse errors through its own load so
    the error path is unchanged.
    """
    try:
        import yaml

        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        return cfg if isinstance(cfg, dict) else None
    except Exception:
        return None


def run(
    config: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        help="Path to the YAML pipeline config.",
    ),
    mode: Mode = typer.Option(
        Mode.mask,
        "--mode",
        "-m",
        help="Operation: mask existing data or generate synthetic data.",
        case_sensitive=False,
    ),
    json_: bool = typer.Option(
        False,
        "--json",
        help="Emit a structured JSON result on stdout. Progress goes to stderr.",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Suppress stdout. Errors still go to stderr; exit code carries success.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable debug-level CLI logs on stderr.",
    ),
    master_key: str = typer.Option(
        None,
        "--master-key",
        envvar="DECOY_MASTER_KEY",
        help=(
            "64-char hex master key for keyed deterministic SYNTHETIC "
            "GENERATION only (generate_columns:). Same key + same "
            "--key-label always yield bitwise-identical generated output "
            "across runs and machines. Reads DECOY_MASTER_KEY env var when "
            "omitted; without either, generation falls back to the legacy "
            "seeded path (per-input deterministic but not portable). "
            "This flag does NOT affect masking -- masking's keyed "
            "determinism is configured separately via '--mask-secret' (or "
            "the pipeline YAML's 'global_settings.mask_secret_ref'), never "
            "this flag or its env var. See: decoy explain keys."
        ),
    ),
    mask_secret: str = typer.Option(
        None,
        "--mask-secret",
        help=(
            "env:NAME or file:/PATH pointing at a >=32-byte mask secret for "
            "keyed masking (mask: strategies -- fpe, hash, date_shift, and "
            "others). Independent of --master-key (which is generation-only). "
            "Sets 'global_settings.mask_secret_ref' for this run; an error if "
            "the YAML already sets it. Explicit flag only -- it deliberately "
            "has NO env var, because the ref it carries (e.g. "
            "'env:DECOY_MASK_SECRET') already indirects through the "
            "environment; a second env layer would absorb the raw exported "
            "secret as this flag's value. See: decoy explain keys."
        ),
    ),
    chunked: bool = typer.Option(
        False,
        "--chunked",
        help=(
            "Stream the source through the engine chunk-by-chunk, for inputs "
            "too large to load whole. Works for mask configs whose every "
            "strategy is value-keyed (hash, fpe, redact, truncate, "
            "text_redact, date_shift, bucketize), plus faker/categorical "
            "when deterministic with an explicit pool_size / categories "
            "declared in config; output is byte-identical to a plain run. "
            "Sources/targets may be CSV or Parquet. See: decoy explain chunked."
        ),
    ),
    chunk_size: int = typer.Option(
        100_000,
        "--chunk-size",
        min=1,
        help="Rows per chunk in --chunked mode.",
    ),
    vault: Path = typer.Option(
        None,
        "--vault",
        help=(
            "Write the token vault (encrypted source-to-masked map for "
            "vault: true columns) to this path. The vault plus the config "
            "re-identify every vaulted value: store them separately and "
            "never alongside the masked output. Needs the engine's vault "
            "extra (cryptography)."
        ),
    ),
    substrate: str = typer.Option(
        None,
        "--substrate",
        envvar="DECOY_SUBSTRATE",
        help=(
            "Execution substrate for --chunked runs: pandas (default) or polars. "
            "Non-chunked (plain) runs always use the engine's pandas adapter "
            "(the V2 unified run_pipeline path); this flag and the DECOY_SUBSTRATE "
            "env var are only consulted for --chunked runs. Setting either on a "
            "plain run emits a warning to stderr and is otherwise ignored. "
            "Cross-substrate outputs are value-equal; CSV bytes may differ only "
            "via Arrow type-width drift, which CSV does not carry."
        ),
    ),
    key_label: str = typer.Option(
        None,
        "--key-label",
        help=(
            "Stable namespace string for the --master-key generation key "
            "hierarchy (synthetic generation only, not masking). Required "
            "when --master-key is set. Pick something durable "
            "(e.g. 'customers_q4'); changing it produces different "
            "generated output. CLI flag only -- PipelineConfig forbids "
            "unknown top-level keys, so there is no YAML equivalent."
        ),
    ),
    evidence_out: Path = typer.Option(
        None,
        "--evidence-out",
        help=(
            "Write a local evidence manifest (JSON) to this path after a "
            "successful run. The manifest records pipeline hash, input/output "
            "file fingerprints, run metadata, and row counts/timings/warnings "
            "where available (these are omitted for --chunked runs). It does "
            "NOT contain raw data values. Use `decoy evidence verify` to check "
            "the manifest against current files. "
            "See: decoy explain evidence (when available)."
        ),
    ),
    notify: list[str] = typer.Option(
        [],
        "--notify",
        help=(
            "Notify a channel after the run reaches its terminal state. "
            "Repeatable. Spec is 'kind:target': webhook:<url>, slack:<url>, "
            "email:<address>. Best-effort: a channel failure never changes "
            "the run's exit code. Webhook signing key from "
            "DECOY_NOTIFY_WEBHOOK_SECRET (unsigned if unset); SMTP from "
            "DECOY_NOTIFY_SMTP_HOST/_PORT/_USER/_PASS/_FROM. Nothing is "
            "persisted to .decoy/workspace.json -- targets and secrets are "
            "flags/env only, never written to disk."
        ),
    ),
    notify_on: NotifyOn = typer.Option(
        NotifyOn.always,
        "--notify-on",
        help="Which terminal outcome(s) to notify on: success, failure, or always.",
        case_sensitive=False,
    ),
) -> None:
    """Run a decoy pipeline from a YAML config.

    Use this to execute a masking or synthetic-generation job described in
    YAML. The engine handles its own logging per the YAML's `logging:`
    section; flags here only affect CLI-side output.
    """
    state = setup_output(json_, quiet, verbose)
    config_str = str(config)

    # Parse --notify specs BEFORE the run (D3): a bad spec is a usage error
    # the user can fix, never a run failure. Notification is a side effect
    # appended after the run reaches its terminal state; it must never
    # change what the run itself does.
    notify_channels = []
    if notify:
        from decoy.notify import NotifySpecError, parse_notify_spec

        try:
            notify_channels = [parse_notify_spec(spec) for spec in notify]
        except NotifySpecError as exc:
            msg = str(exc)
            if state.mode is OutputMode.json:
                emit_json(
                    state,
                    {"command": "run", "status": "error", "config": config_str, "error": msg},
                )
            elif state.mode is not OutputMode.quiet:
                state.err_console.print(error("error:"), msg)
            raise typer.Exit(code=EXIT_USAGE)

    raw_cfg = _load_raw_config(config)
    yaml_mode = _detect_mode(raw_cfg) or mode.value

    resolver = _build_resolver(master_key, key_label, raw_cfg, state)

    # Warn when --substrate (or DECOY_SUBSTRATE) is set on a non-chunked run.
    # The flag is only consulted on the --chunked path; plain runs hardcode
    # the engine's PandasExecutionAdapter. Warning to stderr only; do not
    # suppress in --json mode (json goes to stdout); suppress in --quiet.
    if substrate is not None and not chunked and state.mode is not OutputMode.quiet:
        state.err_console.print(
            warn("warning:"),
            "--substrate is only consulted for --chunked runs; "
            "this plain run uses the engine's pandas adapter.",
        )

    # Sentinels for evidence manifest building (set on successful plain run).
    _ev_config_dict: dict | None = None
    _ev_engine_version: str | None = None
    _ev_row_counts: dict | None = None
    _ev_timings: tuple = ()
    _ev_engine_warnings: tuple = ()
    # Row count for the --notify event; unconditional (unlike _ev_row_counts,
    # which only fills for --evidence-out). None for --chunked runs (no
    # ExecutionResult to count).
    _notify_row_count: int | None = None

    _run_started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    try:
        with spinner(state, f"Running {yaml_mode}..."):
            from decoy_engine import (
                PipelineConfig,
                run_pipeline,
            )
            from decoy_engine import (
                __version__ as engine_version,
            )

            if raw_cfg is not None:
                raw = raw_cfg
            else:
                # The defensive parse failed; re-parse here so the real
                # YAML error surfaces through the normal error path.
                import yaml as _yaml

                raw = _yaml.safe_load(config.read_text(encoding="utf-8"))

            # DE-02 secret-disclosure ROOT guard: validate the RAW YAML
            # `mask_secret_ref` BEFORE PipelineConfig.model_validate(raw). A
            # wrong-type value (list/int/dict/...) would otherwise raise a
            # Pydantic ValidationError whose message echoes `input_value` --
            # i.e. the secret -- and that error is not classified EXIT_USAGE, so
            # it would exit 3 and print the secret via str(exc). Catching it
            # here with a redacted usage error means no mask_secret_ref value
            # (string or not) ever reaches Pydantic's diagnostics. YAML-only:
            # the --mask-secret flag is always a str from Typer.
            _raw_gs = raw.get("global_settings") if isinstance(raw, dict) else None
            # A non-dict `global_settings` (e.g. a YAML list) is malformed AND
            # could smuggle a mask_secret_ref value nested where the dict-path
            # check below can't see it -- Pydantic would then echo that value in
            # its ValidationError. Reject a present-but-non-mapping
            # global_settings here, with a redacted message, before
            # model_validate ever sees the raw structure.
            if _raw_gs is not None and not isinstance(_raw_gs, dict):
                raise _MaskSecretUsageError(
                    "global_settings must be a mapping (a YAML block of "
                    f"key: value pairs), not a {type(_raw_gs).__name__}. "
                    "See: decoy explain keys."
                )
            _raw_ref = _raw_gs.get("mask_secret_ref") if isinstance(_raw_gs, dict) else None
            if _raw_ref is not None and not _is_valid_mask_secret_ref(_raw_ref):
                raise _MaskSecretUsageError(
                    "Invalid global_settings.mask_secret_ref: expected an "
                    "'env:NAME' or 'file:/PATH' reference to a >=32-byte secret. "
                    "It is a REFERENCE, never the raw secret. "
                    "See: decoy explain keys."
                )

            # Catch the schema ValidationError NARROWLY here (not in the
            # run-level except) so ONLY user-config validation maps to
            # EXIT_USAGE; a Pydantic error raised deeper in the engine (e.g.
            # internal registry/capability models) stays an EXIT_RUNTIME defect.
            # str(exc) is safe to surface: the mask_secret_ref ROOT guard above
            # already rejected any secret-bearing structure before this point,
            # and the F8 message cap bounds the rest.
            try:
                from pydantic import ValidationError as _PydanticValidationError

                config_dict = PipelineConfig.model_validate(raw).model_dump()
            except _PydanticValidationError as exc:
                raise _ConfigValidationError(str(exc)) from exc

            # DE-02 Option B (2026-07-15): --mask-secret sets the same
            # `global_settings.mask_secret_ref` slot the YAML can set directly.
            # It feeds run_pipeline's fail-closed KeyProvider resolution AND
            # the --chunked path (both read mask_secret_ref off this same
            # config dict -- _pipeline.py / _chunked.py); one injection point
            # covers both routes.
            #
            # Both-set check uses PRESENCE (`is not None`), not truthiness: a
            # YAML `mask_secret_ref: ""` is still a configured slot, so the flag
            # must not silently override it -- one secret source per run, chosen
            # explicitly. The flag itself has no env var, so `mask_secret is
            # None` here means "flag absent".
            if mask_secret is not None:
                existing_ref = (config_dict.get("global_settings") or {}).get("mask_secret_ref")
                if existing_ref is not None:
                    raise _MaskSecretUsageError(
                        "--mask-secret was passed but the YAML already sets "
                        "global_settings.mask_secret_ref. Set the masking "
                        "secret in exactly one place: drop --mask-secret, or "
                        "remove mask_secret_ref from the config."
                    )
                config_dict.setdefault("global_settings", {})["mask_secret_ref"] = mask_secret

            # Validate + fail-closed guard on the EFFECTIVE ref (whether it came
            # from --mask-secret or the YAML). Keyed off PRESENCE, not
            # truthiness: a configured-but-empty `mask_secret_ref: ""` is a user
            # error, NOT a silent fall-through to an unkeyed (job_seed) run --
            # the engine's resolvers treat empty as no-secret, so truthiness
            # here would let an empty ref emit UNKEYED output pre-GA. A present
            # ref must be a well-formed `env:`/`file:` reference.
            #
            # Never echo the ref VALUE in any error (it may be a raw secret a
            # user pasted by mistake): the message states the expected format
            # only. This also keeps the value out of the --json error payload,
            # which is built from `str(exc)`.
            effective_ref = (config_dict.get("global_settings") or {}).get("mask_secret_ref")
            if effective_ref is not None:
                if not _is_valid_mask_secret_ref(effective_ref):
                    raise _MaskSecretUsageError(
                        "Invalid mask secret: expected an 'env:NAME' or "
                        "'file:/PATH' reference to a >=32-byte secret (from "
                        "--mask-secret or global_settings.mask_secret_ref). It "
                        "is a REFERENCE, never the raw secret. "
                        "See: decoy explain keys."
                    )
                # A mask_secret_ref is only honored by a DE-02 engine; a
                # pre-DE-02 engine has no `keyprovider` module and would
                # SILENTLY IGNORE the ref, emitting job-seed-keyed output
                # (fail-open). The pyproject floor (decoy-engine>=0.4.0, DE-02's
                # release marker) is the first line of defense; this runtime
                # probe is defense-in-depth against a broken or forced install
                # that satisfies the resolver but lacks keyprovider -- refuse
                # the run rather than leak an unkeyed artifact.
                try:
                    import decoy_engine.keyprovider  # noqa: F401
                except ImportError as exc:
                    raise _MaskSecretUsageError(
                        "a mask secret is configured (mask_secret_ref / "
                        "--mask-secret) but the installed decoy-engine is too "
                        "old to honor it -- it has no DE-02 keyprovider, so it "
                        "would silently emit UNKEYED output. Upgrade to "
                        "decoy-engine>=0.4.0 (with DE-02). See: decoy explain keys."
                    ) from exc

            _ev_config_dict = config_dict
            _ev_engine_version = engine_version

            # FC-1 (2026-06-26): run_pipeline is now wired as the single
            # non-chunked entry for all config shapes (mask-only, generate-
            # only, and mixed). The engine handles profiling, planning, the
            # FK graph, and sequencing (generate first, then mask with
            # generate outputs merged into sources). The old split between
            # `generate_tables` and `PandasExecutionAdapter.run` is removed.
            tables_list = config_dict.get("tables") or []
            any_generate = any(
                isinstance(t, dict) and t.get("generate_columns") for t in tables_list
            )

            # Chunked mask streaming is mask-only. Reject loudly when the
            # config has any generate-kind table so the operator gets a clear
            # message instead of silently skipping those tables.
            if chunked and any_generate:
                raise _ChunkedGenerateError(
                    "--chunked is only supported for mask-only configs; "
                    "this config includes generate tables (generate_columns:). "
                    "Run without --chunked to execute a mixed or generate pipeline."
                )

            vault_writer = None
            if vault is not None:
                from decoy_engine import vault_writer_for_config
                from decoy_engine.vault import iter_vault_columns

                if not iter_vault_columns(config_dict):
                    raise _VaultUsageError(
                        "--vault was passed but no column declares vault: true "
                        "in this config. Add `vault: true` to the columns whose "
                        "source values the vault should record."
                    )
                vault_writer = vault_writer_for_config(config_dict)

            if chunked:
                _run_chunked_mask(config_dict, config.parent, chunk_size, substrate, vault_writer)
            else:
                sources = _load_sources_from_config(config_dict, config.parent)
                instance_locale = (config_dict.get("global_settings") or {}).get("default_locale")
                result = run_pipeline(
                    config_dict,
                    sources,
                    engine_version=engine_version,
                    derive_key=resolver,
                    instance_default_locale=instance_locale,
                    vault_writer=vault_writer,
                )
                _write_mask_outputs(config_dict, result, config.parent)
                if evidence_out is not None:
                    _ev_row_counts = {name: len(tbl) for name, tbl in result.outputs.items()}
                _ev_timings = result.timings
                _ev_engine_warnings = result.warnings
                _notify_row_count = sum(len(tbl) for tbl in result.outputs.values())
            if vault_writer is not None:
                vault_writer.write(vault)
    except typer.Exit:
        # CLI QA fix (2026-06-02, F7): an inner call site that raises
        # typer.Exit (e.g. a library that vendored Typer) has already
        # set its intended exit code. Do not swallow it into the
        # EXIT_RUNTIME catch-all below.
        raise
    except Exception as exc:
        # Audit H10 (2026-06-12): dispatch on exception type so scripts
        # can tell "your config is wrong" (EXIT_USAGE, per the
        # exit_codes.py contract) from "the run blew up" (EXIT_RUNTIME).
        # Imported lazily to keep the engine import off the help path.
        from decoy_engine import ConfigError, PipelineValidationError
        from decoy_engine.plan import PlanCompileError

        # DE-02: MaskSecretError lives in the engine's `keyprovider` module,
        # which a pre-DE-02 engine lacks. Import defensively so a missing
        # module never crashes the error handler itself (it would mask the
        # real exception). `()` makes the isinstance check below a harmless
        # no-op when the class is unavailable.
        try:
            from decoy_engine.keyprovider import MaskSecretError as _MaskSecretError

            _mask_secret_error_types: tuple = (_MaskSecretError,)
        except ImportError:
            _mask_secret_error_types = ()

        # OOM checker v1 (Part A): ExecutionError's two capacity-refusal codes
        # get their own exit code and JSON error_kind, distinct from a generic
        # engine crash. Same defensive-import posture as MaskSecretError above
        # -- an engine too old to have ExecutionError at all just never
        # matches, so this never crashes the error handler itself.
        try:
            from decoy_engine.execution import ExecutionError as _ExecutionError

            _execution_error_types: tuple = (_ExecutionError,)
        except ImportError:
            _execution_error_types = ()

        capacity_code: str | None = None
        if isinstance(exc, _execution_error_types) and exc.code in _CAPACITY_CODES:
            capacity_code = exc.code

        if capacity_code is not None:
            _exit_code = EXIT_CAPACITY
        else:
            _exit_code = (
                EXIT_USAGE
                if isinstance(
                    exc,
                    (
                        PlanCompileError,
                        PipelineValidationError,
                        ConfigError,
                        _ChunkedGenerateError,
                        _VaultUsageError,
                        _MaskSecretUsageError,
                        # PipelineConfig.model_validate's ValidationError, caught
                        # narrowly at its call site and re-raised as this typed
                        # error, means the YAML is structurally wrong (unknown key
                        # under extra="forbid", wrong-typed / missing field) -- the
                        # user's config is bad, not a runtime crash. Narrow by
                        # design: a Pydantic error raised deeper in the engine is
                        # NOT reclassified and stays an EXIT_RUNTIME defect.
                        _ConfigValidationError,
                        # DE-02: a bad/missing/weak --mask-secret ref (or YAML
                        # mask_secret_ref) resolves to MaskSecretError (and its
                        # subclasses MissingMaskSecret / WeakMaskSecret /
                        # KeyedStrategyRequiresSecret) -- the operator's config is
                        # wrong, not a runtime crash.
                        *_mask_secret_error_types,
                    ),
                )
                else EXIT_RUNTIME
            )
        # CLI QA fix (2026-06-02, F8): cap the error message at 500
        # chars before emitting through --json. Engine exceptions can
        # quote source-row content verbatim (pandas / pyarrow); without
        # the truncation a single malformed row's value can land in
        # downstream log aggregators.
        error_text = str(exc)
        if len(error_text) > 500:
            error_text = error_text[:500] + "..."

        # PII egress guard (dennis sprint-5 BLOCKER): the raw engine error
        # can quote source-row cell values verbatim (see the comment above),
        # so it MUST NOT ride into an outbound notification, which is POSTed
        # to a third-party webhook / Slack / email. Only the exception TYPE
        # name goes on the wire; the raw error_text stays on local stdout /
        # stderr and the --json envelope below where the operator already
        # sees it. Redaction by construction, mirroring the platform's
        # facts-only alert rule (dispatcher.py:137-142).
        notify_results = _dispatch_run_notifications(
            notify_channels,
            notify_on=notify_on.value,
            status="failure",
            config_path=config_str,
            row_count=None,
            started_at=_run_started_at,
            finished_at=datetime.now(timezone.utc),
            error_summary=type(exc).__name__,
            state=state,
        )

        if state.mode is OutputMode.json:
            # R7: the capacity branch ADDS error_kind/code to this SAME
            # envelope -- command/config/mode/error are never dropped, and
            # this is the one emit_json call site for a run-time failure
            # (no parallel emit that would lose the 500-char cap, the
            # notify dispatch above, or quiet/verbose handling below).
            payload = {
                "command": "run",
                "status": "error",
                "config": config_str,
                "mode": yaml_mode,
                "error": error_text,
            }
            if capacity_code is not None:
                payload["error_kind"] = "capacity"
                payload["code"] = capacity_code
            if notify_channels:
                payload["notify"] = notify_results
            emit_json(state, payload)
        elif state.mode is not OutputMode.quiet:
            if capacity_code is not None:
                state.err_console.print(error("capacity:"), error_text)
                state.err_console.print(
                    " ",
                    hint("hint:"),
                    "this job needs more memory than this host has; use a larger "
                    "tier or reduce the job.",
                )
            else:
                state.err_console.print(error("error:"), error_text)
                state.err_console.print(
                    " ", hint("hint:"), "rerun with --verbose for the full traceback."
                )
        if state.verbose:
            state.err_console.print_exception()
        raise typer.Exit(code=_exit_code)

    elapsed = time.perf_counter() - started

    # Write evidence manifest if --evidence-out was given. Written for BOTH
    # plain and chunked runs: the input/output fingerprints (the drift-detection
    # core) are always populated. Chunked runs return no ExecutionResult, so
    # row_counts/timings/warnings are empty for them (documented in the flag help).
    if evidence_out is not None and _ev_config_dict is not None:
        from decoy.cli.evidence import build_manifest

        manifest = build_manifest(
            pipeline_path=config,
            config_dict=_ev_config_dict,
            run_result={"row_counts": _ev_row_counts or {}},
            cli_version=_cli_version,
            engine_version=_ev_engine_version or "unknown",
            key_label=key_label,
            timings=_ev_timings,
            engine_warnings=_ev_engine_warnings,
        )
        evidence_out.write_text(_json.dumps(manifest, indent=2), encoding="utf-8")
        if state.mode is not OutputMode.json and state.mode is not OutputMode.quiet:
            from decoy.ui.theme import success as _success

            state.console.print(_success("Evidence:"), str(evidence_out))

    # Record the run in the local catalog when a workspace is found upward from
    # cwd. Silent on failure -- catalog errors never kill the run itself.
    # entry_type='run' so `decoy jobs list` can filter to run entries.
    _record_run_to_catalog(
        config_path=str(config),
        mode=yaml_mode,
        elapsed_s=round(elapsed, 3),
        cli_version=_cli_version,
        engine_version=_ev_engine_version or "unknown",
        evidence_path=str(evidence_out) if evidence_out is not None else None,
        verbose=state.verbose,
    )

    notify_results = _dispatch_run_notifications(
        notify_channels,
        notify_on=notify_on.value,
        status="success",
        config_path=config_str,
        row_count=_notify_row_count,
        started_at=_run_started_at,
        finished_at=datetime.now(timezone.utc),
        error_summary=None,
        state=state,
    )

    if state.mode is OutputMode.json:
        payload = {
            "command": "run",
            "status": "ok",
            "config": config_str,
            "mode": yaml_mode,
            "elapsed_s": round(elapsed, 3),
        }
        if notify_channels:
            payload["notify"] = notify_results
        emit_json(state, payload)
        return

    if state.mode is OutputMode.quiet:
        return

    facts = [
        ("Pipeline", config.name),
        ("Mode", yaml_mode),
        ("Elapsed", f"{elapsed:.2f}s"),
    ]
    if notify_channels:
        delivered = sum(1 for r in notify_results if r["delivered"])
        facts.append(("Notify", f"{delivered}/{len(notify_results)} delivered"))

    render_card(
        state,
        command="decoy run",
        facts=facts,
        next_hint=_next_hint_for_run(raw_cfg, mode),
        status="ok",
    )


def _build_resolver(master_key_hex: str | None, key_label: str | None, raw_cfg: dict | None, state):
    """Construct the engine-facing ``derive_key`` resolver, or None when no
    master key was supplied. Keeps the legacy seeded fallback default so
    runs without a key behave exactly as before.

    ``raw_cfg`` is accepted (and unused) for call-site symmetry with the
    other pre-flight helpers that share the one parsed YAML dict; it used
    to feed a top-level YAML ``key_label:`` reader. This function runs
    early (the run() body calls it ~L307), BEFORE ``PipelineConfig
    .model_validate`` (~L371), so that reader could observe a top-level
    ``key_label:`` value -- but model_validate then runs on the same raw
    dict and PipelineConfig's ``extra="forbid"`` rejects the unknown field,
    so a config carrying it could never reach a successful run regardless
    of what the reader returned. The reader was therefore dead and
    misleading; it is removed. ``--key-label`` is the only live source
    (doc/config mismatch fix, see `decoy explain keys`).
    """
    if not master_key_hex:
        return None

    raw = master_key_hex.strip()
    if raw.lower().startswith("0x"):
        raw = raw[2:]
    try:
        master = bytes.fromhex(raw)
    except (ValueError, binascii.Error) as exc:
        raise typer.BadParameter(
            f"--master-key must be valid hex ({exc}). Generate one via: "
            "python -c 'import secrets; print(secrets.token_hex(32))'"
        )
    if len(master) != 32:
        raise typer.BadParameter(f"--master-key must decode to 32 bytes (got {len(master)}).")

    if not key_label:
        raise typer.BadParameter(
            "--master-key requires a --key-label. Pick a stable namespace "
            "string like 'customers_q4'."
        )

    from decoy_engine import make_key_resolver

    return make_key_resolver(master, key_label)


def _detect_mode(raw_cfg: dict | None) -> str | None:
    """Infer mode from the YAML's tables, or return None if not determinable.

    FC-1 (2026-06-02) dropped the top-level `mode:` field; the engine now
    infers per-table kind from `columns` (mask) vs `generate_columns`
    (generate) presence. This helper picks one label for the spinner / the
    JSON envelope's `mode` field: returns "generate" iff EVERY table is
    generate-kind; "mask" iff at least one mask-kind table is present
    (mixed configs label as "mask" because the mask back-half is the
    workflow that touches the operator's source data). The choke-point
    validator still rejects malformed configs.
    """
    if not isinstance(raw_cfg, dict):
        return None
    tables = raw_cfg.get("tables") or []
    if not isinstance(tables, list):
        return None
    has_mask = any(isinstance(t, dict) and t.get("columns") for t in tables)
    has_gen = any(isinstance(t, dict) and t.get("generate_columns") for t in tables)
    if has_mask:
        return "mask"
    if has_gen:
        return "generate"
    return None


def _next_hint_for_run(raw_cfg: dict | None, mode: "Mode") -> str | None:
    """Best-effort follow-up hint based on the YAML's output path."""
    try:
        out = raw_cfg.get("output", {}).get("path") if isinstance(raw_cfg, dict) else None
        if out:
            return f"head {out}"
    except Exception:
        pass
    return None


def _run_chunked_mask(
    config_dict: dict,
    base_dir: Path,
    chunk_size: int,
    substrate: str | None = None,
    vault_writer=None,
) -> None:
    """WS4 chunked mask path: stream each mask table's source through
    `decoy_engine.run_mask_pipeline_chunked`, writing output per chunk.

    The engine's `check_chunked_compatibility` rejects anything that is
    not value-keyed (PlanCompileError -> EXIT_USAGE via the H10 typed
    dispatch), so a chunked run that starts is guaranteed byte-identical
    to a plain run of the same config for CSV targets. Parquet targets
    are VALUE-equal to a plain run; their file bytes are stable for a
    fixed (chunk_size, pyarrow version) but not across chunk sizes,
    because each chunk writes one row group.

    Formats: CSV and parquet, on either side independently (suffix
    picks the reader/writer, mirroring the plain path's free mixing).
    Parquet reads stream via ParquetFile.iter_batches; CSV reads keep
    the dtype=str contract of the plain path, so a csv -> parquet run
    produces an all-string schema.

    `substrate` None keeps the chunked default (pandas, the byte-stable
    contract this mode shipped with); an explicit value selects the
    adapter via the engine's `select_execution_adapter`."""
    from decoy_engine import __version__ as engine_version
    from decoy_engine import run_mask_pipeline_chunked
    from decoy_engine.execution import select_execution_adapter

    adapter = select_execution_adapter(substrate=substrate) if substrate is not None else None

    sources = config_dict.get("sources") or {}
    targets = config_dict.get("targets") or {}
    for table_entry in config_dict.get("tables") or []:
        if not isinstance(table_entry, dict) or not table_entry.get("columns"):
            continue
        name = table_entry.get("name")
        src_spec = sources.get(name) if isinstance(sources, dict) else None
        tgt_spec = targets.get(name) if isinstance(targets, dict) else None
        if not isinstance(src_spec, dict) or not isinstance(src_spec.get("path"), str):
            continue
        src_path = _resolve_path(src_spec["path"], base_dir)
        if not isinstance(tgt_spec, dict) or not isinstance(tgt_spec.get("path"), str):
            continue
        out_path = _resolve_path(tgt_spec["path"], base_dir)

        masked_iter = run_mask_pipeline_chunked(
            config_dict,
            _iter_source_chunks(src_path, chunk_size),
            table=name,
            engine_version=engine_version,
            adapter=adapter,
            vault_writer=vault_writer,
        )
        _write_chunked_output(masked_iter, out_path, src_path)


def _iter_source_chunks(src_path: Path, chunk_size: int):
    """Yield pa.Tables of at most `chunk_size` rows from a CSV or parquet file.

    Parquet batches can come back SHORTER than chunk_size at row-group
    boundaries; that is fine and must stay fine -- chunked output is
    chunking-invariant by the engine's parity contract, so nobody should
    "fix" the short batches by re-buffering.
    """
    import pandas as pd
    import pyarrow as pa

    if src_path.suffix.lower() == ".parquet":
        import pyarrow.parquet as pq

        parquet_file = pq.ParquetFile(str(src_path))
        for batch in parquet_file.iter_batches(batch_size=chunk_size):
            yield pa.Table.from_batches([batch])
        return
    for df in pd.read_csv(src_path, dtype=str, chunksize=chunk_size):
        yield pa.Table.from_pandas(df, preserve_index=False)


def _write_chunked_output(masked_iter, out_path: Path, src_path: Path) -> None:
    """Stream masked chunks to `out_path`, format picked by its suffix."""
    if out_path.suffix.lower() == ".parquet":
        import pyarrow.parquet as pq

        writer = None
        try:
            for masked in masked_iter:
                if writer is None:
                    writer = pq.ParquetWriter(str(out_path), masked.schema)
                writer.write_table(masked)
            if writer is None and src_path.suffix.lower() == ".parquet":
                # Empty parquet source: emit a valid zero-row file with the
                # source schema, matching what a plain run writes. (An empty
                # CSV source writes nothing, same as the CSV target path.)
                schema = pq.ParquetFile(str(src_path)).schema_arrow
                writer = pq.ParquetWriter(str(out_path), schema)
        finally:
            if writer is not None:
                writer.close()
        return
    first = True
    for masked in masked_iter:
        masked.to_pandas().to_csv(out_path, index=False, header=first, mode="w" if first else "a")
        first = False


def _resolve_path(raw_path: str, base_dir: Path) -> Path:
    """Resolve a YAML path against the config's directory.

    Mirrors `decoy plan`'s source-loading behavior: relative paths in the
    YAML resolve relative to the YAML file's directory, not the CWD.
    Absolute paths pass through unchanged.
    """
    p = Path(raw_path)
    return p if p.is_absolute() else (base_dir / p).resolve()


def _load_sources_from_config(config_dict: dict, base_dir: Path) -> dict:
    """Read each `sources[table]` into a `dict[str, pa.Table]`.

    Accepts CSV and Parquet by file extension. Sources without a `path`
    field are skipped (the engine treats absent tables as empty, but
    the mask spine will error on a missing source if the plan needs it;
    leave that error to the engine layer).
    """
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq

    out: dict[str, pa.Table] = {}
    sources = config_dict.get("sources") or {}
    if not isinstance(sources, dict):
        return out
    for table_name, src in sources.items():
        if not isinstance(src, dict):
            continue
        raw_path = src.get("path")
        if not isinstance(raw_path, str):
            continue
        path = _resolve_path(raw_path, base_dir)
        suffix = path.suffix.lower()
        if suffix == ".parquet":
            out[table_name] = pq.read_table(str(path))
        else:
            df = pd.read_csv(path, dtype=str)
            out[table_name] = pa.Table.from_pandas(df, preserve_index=False)
    return out


def _write_mask_outputs(config_dict: dict, result, base_dir: Path) -> None:
    """Write each declared target from the V2 ExecutionResult.

    CLI.3 commit 2 (2026-06-02) fix: the V2 ExecutionResult exposes
    masked tables on `outputs` (dict[table_name -> pa.Table]), not
    `tables`. The V2 `targets:` block is a dict keyed by table name,
    not a list of `{table, path}` entries. Pre-fix the CLI silently
    skipped the writer (no errors, no output file), surfaced by the
    demo + test_output_modes smoke runs.

    The pandas adapter returns masked tables in-memory; the polars
    adapter writes them via its own target-writer. CLI.1 bridges the
    pandas path with this helper. Format inferred from the path
    extension (csv or parquet).
    """
    targets = config_dict.get("targets") or {}
    if not isinstance(targets, dict):
        return
    outputs = getattr(result, "outputs", None)
    if not isinstance(outputs, dict):
        return
    for table_name, entry in targets.items():
        if not isinstance(entry, dict):
            continue
        raw_path = entry.get("path")
        if not isinstance(raw_path, str):
            continue
        table = outputs.get(table_name)
        if table is None:
            continue
        path = _resolve_path(raw_path, base_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        suffix = path.suffix.lower()
        if suffix == ".parquet":
            import pyarrow.parquet as pq

            pq.write_table(table, str(path))
        else:
            table.to_pandas().to_csv(path, index=False)


def _record_run_to_catalog(
    *,
    config_path: str,
    mode: str,
    elapsed_s: float,
    cli_version: str,
    engine_version: str,
    evidence_path: str | None,
    verbose: bool = False,
) -> None:
    """Record a completed local run in the workspace catalog (SP-18b).

    Silently skipped when:
      - No .decoy/ workspace is found upward from cwd.
      - The catalog write fails for any reason.

    The catalog entry uses entry_type='run' so `decoy jobs list` can filter
    to run entries. The evidence_path (if present) is stored in metadata so
    `decoy report show <run-id>` can locate the evidence manifest.

    This is a best-effort convenience record. It must never cause the run
    itself to fail. Pass verbose=True to emit a stderr breadcrumb on failure
    so catalog issues are visible when debugging.
    """
    import json as _j
    import sys as _sys
    from datetime import datetime, timezone
    from pathlib import Path as _Path
    from uuid import uuid4

    try:
        from decoy.cli.catalog import _open_catalog
        from decoy.cli.project import _dotdecoy, _resolve_workspace

        root = _resolve_workspace(None)
        if root is None:
            return
        ws_json = _dotdecoy(root) / "workspace.json"
        if not ws_json.exists():
            return

        run_id = str(uuid4())
        entry_id = str(uuid4())
        now = datetime.now(tz=timezone.utc).isoformat()
        name = _Path(config_path).stem

        meta = {
            "run_id": run_id,
            "status": "ok",
            "mode": mode,
            "elapsed_s": elapsed_s,
            "config_path": config_path,
            "engine_version": engine_version,
            "cli_version": cli_version,
            "run_timestamp": now,
        }
        if evidence_path is not None:
            meta["evidence_path"] = evidence_path

        conn = _open_catalog(root)
        try:
            conn.execute(
                """
                INSERT INTO entries
                    (id, entry_type, name, path, recorded_at, metadata, sensitivity_class)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    entry_id,
                    "run",
                    name,
                    evidence_path or config_path,
                    now,
                    _j.dumps(meta),
                    "evidence-safe",
                ],
            )
        finally:
            conn.close()
    except Exception as _exc:
        # Never propagate -- catalog recording is best-effort.
        if verbose:
            print(
                f"[decoy] warning: catalog write failed (run will not appear in "
                f"`decoy jobs list`): {_exc}",
                file=_sys.stderr,
            )


def _dispatch_run_notifications(
    channels: list,
    *,
    notify_on: str,
    status: str,
    config_path: str,
    row_count: int | None,
    started_at,
    finished_at,
    error_summary: str | None,
    state,
) -> list[dict]:
    """Best-effort post-run notification fan-out (N3).

    Called AFTER the run reaches its terminal state (both the success tail
    and the failure tail call this). Never raises and never changes the
    run's own exit code: a channel failure only logs a stderr warning and
    is reflected in the returned per-channel results (D2 -- "alerting must
    never take a job down", mirrors dispatcher.py:14-17). Returns `[]`
    when no --notify channels were configured, or when --notify-on filters
    out this outcome.
    """
    if not channels:
        return []

    from decoy.notify import SmtpConfig, build_run_event, dispatch, should_notify

    if not should_notify(notify_on, status):
        return []

    try:
        event = build_run_event(
            status=status,
            config_path=config_path,
            row_count=row_count,
            started_at=started_at,
            finished_at=finished_at,
            error_summary=error_summary,
        )
        webhook_secret = os.environ.get("DECOY_NOTIFY_WEBHOOK_SECRET")
        smtp = SmtpConfig(
            host=os.environ.get("DECOY_NOTIFY_SMTP_HOST", ""),
            port=int(os.environ.get("DECOY_NOTIFY_SMTP_PORT") or 587),
            user=os.environ.get("DECOY_NOTIFY_SMTP_USER") or None,
            password=os.environ.get("DECOY_NOTIFY_SMTP_PASS") or None,
            from_addr=os.environ.get("DECOY_NOTIFY_SMTP_FROM") or None,
        )
        results = dispatch(event, channels, webhook_secret=webhook_secret, smtp=smtp)
        if state.mode is not OutputMode.quiet:
            for r in results:
                if not r.delivered:
                    detail = f": {r.detail}" if r.detail else "."
                    state.err_console.print(
                        warn("warning:"),
                        f"notify {r.kind} to {r.target_host} did not deliver{detail}",
                    )
        return [
            {"kind": r.kind, "delivered": r.delivered, "target_host": r.target_host}
            for r in results
        ]
    except Exception as exc:  # notify must never take the run down (D2)
        if state.mode is not OutputMode.quiet:
            state.err_console.print(warn("warning:"), f"notify dispatch failed: {exc}")
        return []


# The epilog is wired by __main__ at command registration time; the
# docstring on `run` stays as the help body.
RUN_EPILOG = _RUN_EPILOG
