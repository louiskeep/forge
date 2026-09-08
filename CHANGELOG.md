# Changelog

All notable changes to the `decoy-cli` PyPI distribution (which ships
the `decoy` import package + the `decoy` console script) land here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
version numbers follow the [versioning policy](docs/release/versioning.md).

## [Unreleased]

### Changed (OOM checker recalibration: build-floor refusal is now advisory, capacity refusal is fan-in-only, 2026-09-08)

- **`FIT` no longer means "clears the budget."** The engine's OOM checker
  paired with this release (`fix/ooc-preflight-overreject-recalibration`)
  redefines its public `CapacityVerdict.FIT` to mean "no hard impossibility
  detected." The measured completion-cap data showed the old flat
  build-floor refusal was over-rejecting jobs that would have completed;
  that refusal is now a WARNING with a recommended host size, not a job
  refusal. The only capacity refusal left is fan-in (co-live DuckDB
  instances that cannot fit even a 1 MB `memory_limit` under the budget) --
  see the updated `EXIT_CAPACITY` doc comment in `exit_codes.py`.
- **`decoy preflight` renders the advisory as a warning, not a green pass.**
  When the engine estimate reports `FIT` with `warned=True`, preflight now
  emits a `capacity.out_of_core_fk` finding with `status: "warn"` (both
  human and `--json` output) instead of folding it into a silent pass --
  `--fail-on-warning` governs whether that alone exits nonzero; default
  execution still proceeds. A fan-in impossibility discovered while the
  engine resolves its memory budget (previously mis-rendered, if at all) now
  reliably surfaces as a `capacity.out_of_core_fk` FAIL and exits
  `EXIT_CAPACITY`, matching `decoy run`'s behavior for the same case.
- **Legacy-engine compat kept.** `out_of_core_insufficient_memory` (the old
  build-floor hard-fail code) stays recognized by `decoy run`'s capacity
  matcher for an older, not-yet-recalibrated engine; a current engine never
  raises it.

### Added (OOM checker v1: labeled capacity refusal in `decoy run` + a pre-run capacity check in `decoy preflight`, 2026-07-24)

- **New exit code `EXIT_CAPACITY = 5`.** The engine's out-of-core-FK
  memory gate has always refused a job it predicts will not fit; the CLI
  used to fold that refusal into the generic `EXIT_RUNTIME` path, printed
  as a bare `error:` line with no distinct signal a script could switch
  on. `decoy run` now matches the engine's two capacity-refusal codes
  (`out_of_core_insufficient_memory`, `out_of_core_fanin_exceeds_budget`)
  narrowly, prints a `capacity:`-labeled line, and exits `EXIT_CAPACITY`;
  `--json` adds `error_kind: "capacity"` and `code` to the existing error
  envelope (`command`/`config`/`mode`/`error` are unchanged). Any other
  `ExecutionError`, and any config error, keep their existing exit codes.
- **`decoy preflight` predicts the same refusal before a run starts.** A
  new capacity section calls the engine's `estimate_job_capacity` (added
  this cycle) against the SAME evaluator the mid-run gate uses, so the two
  can't drift apart. Renders one of four states -- `capacity: OK`,
  `capacity: INSUFFICIENT` (exits `EXIT_CAPACITY`, not the generic
  usage-error path), `capacity: not checked` (RAM undetectable, an
  unpriceable CSV row count, or an engine too old to have the estimator),
  `capacity: not applicable` (this job would not take the out-of-core-FK
  route) -- without running the pipeline. `--json` carries a structured
  `capacity` block plus a `code` field on the matching `checks` entry, so
  scripts never need to parse the message text.
- **Honest scope, stated up front (v1):** this checks ONE gate, the
  out-of-core-FK route's resident-memory floor. `decoy run` fully loads
  every source into memory before it calls the engine at all, so an
  ingestion `MemoryError` or an OS OOM-kill happens before this gate ever
  runs, and neither command's capacity check covers that or the generate
  path. `decoy explain exit-codes` and both commands' help text say so.
- The engine floor stays `decoy-engine>=0.5.0`; an older, already-installed
  engine without `estimate_job_capacity` degrades `decoy preflight`'s
  capacity section to `not checked` rather than failing.

### Fixed (`decoy unmask` console summary is now a complete census, 2026-07-15)

- **`decoy unmask`'s human-readable summary (non-`--json`) now accounts for
  every column, so its counts can no longer silently lie.** Previously the
  summary tallied only `reversed` / `irreversible` / `untouched` (plus
  `vault_reversed` when a `--vault` was passed), so three genuine per-column
  outcomes the engine emits vanished from the arithmetic entirely:
  - `reversed_unverified` -- an FPE column reversed under the non-secret
    `job_seed` fallback (no `mask_secret_ref` at mask time). A masked PAN that
    was in fact decrypted back to plaintext used to show as
    `"0 column(s) reversed, 3 irreversible, 0 untouched."` -- the column
    dropped out of every bucket and the "FPE is unauthenticated -- a wrong key
    yields plausible but WRONG plaintext" caveat never printed.
  - `vault_miss` -- a vaulted column the supplied vault could not reverse (the
    note printed but the count stayed at zero, so the totals did not add up).
  - `table_missing` -- other configured tables when a multi-table config is
    unmasked one file at a time; these were neither counted nor noted.

  The summary is now a complete census: the six present-column statuses
  (`reversed`, `reversed_unverified`, `vault_reversed`, `vault_miss`,
  `irreversible`, `untouched`) always sum to the number of real columns, each
  non-empty secondary bucket is surfaced (`, N reversed (unverified)`,
  `, N vault-reversed`, `, N vault-miss`), and absent tables are reported in
  their own clause (`, N configured table(s) not in this input`) rather than
  miscounted as columns. Every reversible/miss/absent status also prints its
  detail as a `note:`. `--json` output was already complete and is unchanged.

### Fixed (`decoy init` scaffold runnability + config exit codes, 2026-07-15)

- **`decoy init <file>` now scaffolds configs that actually run.** The
  column-aware scaffolder emits real `provider_config:` keys instead of a
  phantom `params:` block (which `ColumnConfig`'s `extra="forbid"` always
  rejected), AND now emits the `namespace:` field that `hash`, `fpe`, and
  `date_shift` require at runtime (`ColumnConfig.namespace` defaults to
  `None` with no compile-time check, so a missing namespace used to pass
  `decoy validate config` and only fail at `decoy run` with exit 3). `fpe`
  scaffolds for PAN/credit-card columns now also set
  `provider_config: {charset: digits, validate_luhn: true}` so masked PANs
  stay Luhn-valid, matching the bundled PCI template.
- **A malformed pipeline config now exits `EXIT_USAGE` (1), not
  `EXIT_RUNTIME` (3).** `decoy run` catches `PipelineConfig.model_validate`'s
  `ValidationError` narrowly at the validation call site and reclassifies it
  as a usage error -- the operator's YAML is wrong, not an engine crash.
- **A non-dict `global_settings` (e.g. a YAML list) is now rejected before
  schema validation**, with a redacted message, instead of reaching
  `PipelineConfig.model_validate` -- which would otherwise echo any value
  nested inside it (including a smuggled `mask_secret_ref`) via Pydantic's
  `input_value` diagnostics.

### Added (DE-02 keyed-masking CLI surface, 2026-07-15)

- **`decoy run --mask-secret <ref>`**: a first-class flag for keyed masking.
  It sets `global_settings.mask_secret_ref` for the run, feeding the engine's
  DE-02 KeyProvider (the `env:NAME` / `file:/PATH` reference to a >=32-byte
  secret) on both the plain and `--chunked` paths. Independent of
  `--master-key`, which stays generation-only -- the two are separate secrets
  and neither affects the other. Explicit flag only (deliberately no env var,
  since the ref it carries already indirects through the environment).
  Passing both `--mask-secret` and a YAML `mask_secret_ref`, or an empty /
  malformed ref, is a usage error (`EXIT_USAGE`), never a silent unkeyed run.
- **Engine floor raised to `decoy-engine>=0.4.0`** (DE-02's release marker) for
  the DE-02 keyprovider module. The floor now guarantees `decoy_engine.keyprovider`
  is present; a configured mask secret is additionally guarded at run time as
  defense-in-depth: if a broken or forced install lacks `decoy_engine.keyprovider`,
  `decoy run` refuses the run rather than let the engine silently emit UNKEYED
  output.

### Fixed (DE-02 keyed-masking CLI surface, 2026-07-15)

- A bad / missing / weak mask-secret reference (engine `MaskSecretError` and
  its subclasses) now exits `EXIT_USAGE` (the operator's config is wrong)
  instead of `EXIT_RUNTIME`.
- `decoy explain keys` documents `--mask-secret`, corrects the resolver owner
  (the ENGINE resolves the ref, not the CLI), clarifies that an unkeyed
  (job_seed) masking run is reproducible but NOT re-identification-safe
  (the job seed is public, not a confidentiality key), and notes the GA
  fail-closed behavior (a keyed strategy with no resolved secret is rejected
  at GA, per `decoy_engine.release.is_pre_ga()`).

### Added (Sprint 5 CLI closure, 2026-07-04)

- **`decoy validate distribution <source> <output>`**: recompute distribution
  fidelity between a pre-mask/pre-generate source CSV and its post-run output.
  A thin CLI surface over the engine's `compute_quality_report` +
  `apply_quality_policy` (no CLI-side metric). Flags: `--joint a,b`
  (repeatable), `--generate` (drops the row-parity expectation), `--config`
  (names each column's strategy so intentional loss is not flagged as
  accidental drift), `--policy` / `--mode` / `--min-grade` / `--min-score`,
  `--report-out`. A `fail` policy verdict exits `EXIT_FINDINGS` (4), matching
  `decoy storm integrity`'s data-audit exit-code contract.

- **`decoy run --notify <kind:target>`** (repeatable) + **`--notify-on
  {success,failure,always}`**: send a best-effort notification after a run
  reaches its terminal state. Channels: `webhook` (HMAC-signed when
  `DECOY_NOTIFY_WEBHOOK_SECRET` is set), `slack` (incoming webhook), `email`
  (`DECOY_NOTIFY_SMTP_HOST/_PORT/_USER/_PASS/_FROM`). Targets and secrets are
  flags/env only, never persisted to `.decoy/workspace.json`. A channel
  failure never changes the run's exit code. Payloads carry facts only
  (status, row count, config path, timings); the raw engine error is NEVER
  put on the wire (only the exception type name), so a failed run cannot
  egress source-row values to a third-party channel.

### Changed (Sprint 5 CLI closure, 2026-07-04)

- **`decoy validate` is now a command group**: `decoy validate <cfg>` becomes
  **`decoy validate config <cfg>`** (the config schema check is unchanged; it
  moved under the `config` subcommand so `distribution` can sit beside it).
  Pre-GA hard-delete break: no back-compat shim. Note: `validate config
  --fail-on-warning` exits `2` (its long-standing code) while `validate
  distribution --fail-on-warning` exits `EXIT_FINDINGS` (4); the two
  subcommands intentionally use different warning exit codes for their
  different domains (config well-formedness vs. data-fidelity findings).
### Fixed (#15, 2026-07-04)

- **HIPAA template disguise-version drift (under-masking regression)**:
  the bundled `hipaa` template's `mrn` column still used FPE
  `charset: alphanum` after the engine's HIPAA disguise widened it to
  `ALPHANUM` (2026-06-29, passthrough-leak fix: FPE's
  `preserve_separators` mode passes characters outside the configured
  charset through unchanged, so lowercase-only `alphanum` let uppercase
  letters in institution-specific MRN formats, e.g. `MRN12345A`, leak
  in the clear). The template was never re-derived against the new
  disguise version, so the CLI's default HIPAA scaffold shipped the
  same under-masking defect. Fixed by widening the template's `mrn`
  charset to `ALPHANUM` and bumping its `x-derived-from-disguise`
  pin to `hipaa@2026-06-29`. The `test_template_disguise_drift.py`
  guard (`test_pinned_version_matches_live_disguise` +
  `test_every_mapped_column_matches_its_disguise_rule`) now passes.

### Added (SP-16 CLI foundation, 2026-06-28)

- **`decoy validate config --fail-on-warning`**: exits non-zero (code 2) when
  any advisory warning fires. Enables CI gates that treat warnings as blocking.
  Current warnings: output target file already exists (overwrite advisory).

- **`decoy validate config --json` multi-message output**: the JSON envelope
  now includes a `messages` list with ALL validation messages
  (`severity`/`code`/`message`/`location`), not just the first error string.
  Pydantic `ValidationError` with multiple field failures now surfaces all of
  them at once. Backward-compatible: the top-level `error` string key is
  preserved for error responses.

- **`decoy storm analyze --format parquet|fixed-width|delimited`**: explicit
  format selector for the STORM analyzer. Format is also inferred from file
  extension (`.parquet`/`.pq` -> parquet; `.fwf`/`.dat`/`.fixed`/`.fw` ->
  fixed-width; everything else -> delimited/CSV).

- **`decoy storm analyze` Parquet support**: `.parquet` files are now read via
  `pd.read_parquet` (pyarrow is a core engine dependency). Sampling
  strategies (`--rows`, `--strategy`) apply after load.

- **`decoy storm analyze --layout <layout.yaml>`**: fixed-width input support
  behind an explicit layout spec (column name + start offset + width). Layout
  can be YAML or JSON. Fixed-width without `--layout` fails closed with a
  clear, actionable error -- column boundaries are ambiguous without a spec.

### Added (capability gaps, 2026-06-12)

- **`decoy run --chunked [--chunk-size N]`** (streaming). Streams each
  mask table's CSV source through the engine chunk-by-chunk (default
  100k rows) for inputs too large for memory; output is byte-identical
  to a plain run. Only value-keyed strategies qualify (hash, fpe,
  redact, truncate, text_redact, date_shift, bucketize); anything else
  exits 1 with the engine's typed rejection before any rows process.
  CSV sources only in v1.

- **`decoy fit` verb** (statistical synthesis). Fits a
  distribution-snapshot/v1 artifact from a source CSV
  (`--parse-dates` for datetime columns, repeatable `--joint a,b` for
  the contingency tables `condition_on` needs). The snapshot is what
  `type: statistical` generate columns reference via `snapshot_file`;
  `decoy validate config` now rejects configs whose snapshot artifact is
  missing or incompatible (engine check row 12).

- **`decoy unmask` verb** (detokenization). Recovers `strategy: fpe`
  columns from a masked CSV using the same pipeline config the mask run
  used; the config's seed + per-column namespace re-derive the Feistel
  key (engine SEED_PROTOCOL_VERSION 5, single key per seed+namespace).
  One-way strategies (hash, redact, faker, ...) pass through unchanged
  with an `irreversible` entry in the per-column report (`--json` for
  the structured form). SECURITY: the config now functions as the
  decryption key for its fpe columns; handle accordingly.

### Fixed (audit remediation, 2026-06-12)

Findings from the 2026-06-11 full-codebase audit.

- **hipaa/pci/gdpr templates rewritten and no longer dead-on-arrival**
  (audit H5 + H12). All three crashed at `decoy run` on any input
  (faker on the non-poolable uuid provider). Templates are now derived
  from the engine's dated disguises and carry an
  `x-derived-from-disguise: <id>@<version>` marker enforced by a
  drift-guard test: hipaa upgrades ssn/mrn/account/vehicle to
  format-preserving encryption and dates to keyed date_shift (joins and
  intervals survive); pci PANs are FPE'd with Luhn-valid output and
  transaction ids hash-pseudonymised; gdpr device ids
  hash-pseudonymised per Art 4(5). A new E2E net runs EVERY bundled
  template against synthesized data on every CI run.
- **`decoy validate config` now runs the engine's config-only plan checks**
  (audit H5): unknown providers, non-poolable faker providers, and
  missing deterministic namespaces fail validate with the typed code
  instead of passing schema-only and crashing at run.
- **Typed exit codes** (audit H10): engine config errors
  (PlanCompileError / PipelineValidationError / ConfigError) exit
  EXIT_USAGE(1) per the documented contract; runtime crashes stay
  EXIT_RUNTIME(3).
- **Mixed mask+generate configs rejected** (audit H11): previously they
  exited 0 while silently writing no output for the generate tables.
- **`storm integrity` exit 4 is reachable** (engine audit C1/H6): a
  masked file identical to its source now fails with a residual_pii
  'fail'; E2E locks both directions.
- **Config parsed once per `decoy run`** (audit L3; was up to 4 parses).
- **ruff clean** (audit L4: 13 errors cleared, zero-error gate).

### Added

- OSS.3 packaging metadata: PyPI Trove classifiers, keywords, project
  URLs, and the distribution rename to `decoy-cli` (per Q-OSS-1 RESOLVED
  2026-06-01). The import package + console script + the `decoy-engine`
  dependency are unchanged.
- This `CHANGELOG.md` itself, plus the `docs/release/versioning.md`
  semver policy doc.
- **Generated CLI reference** (`docs/cli-reference.md`, produced by
  `python -m typer decoy.__main__ utils docs`). A
  `tests/unit/test_cli_surface.py` drift guard fails CI when the command or
  flag surface changes without the reference being regenerated.

### Changed

- Help-text accuracy pass: root and `storm test` help no longer reference the
  old `storm scan` verb (now `storm analyze`); the `templates` help example no
  longer shows the removed `graph` template; `explain modes` help no longer
  lists the removed `convert`/`graph` modes; `explain transforms` notes the full
  12-strategy set instead of claiming "eight"; `CODEMAP.md` drops the removed
  `forecast` command / `forecast.py`.
- PyPI distribution name: `decoy` -> `decoy-cli` (Q-OSS-1). Existing
  `pip install decoy` continues to work today (the bare name is still
  reserved, just not pursued as a launch-blocker); future installs
  should use `pip install decoy-cli`.

## [0.1.0] - 2026-06-02

The first publishable cut of the CLI. Not yet pushed to the real PyPI
index; first publish lands with OSS.7.

### Added

- OSS.1 release gate: `.github/workflows/release-smoke.yml` builds the
  wheel + installs into a clean Python 3.10 / 3.11 / 3.12 venv + runs
  `decoy --version`, `decoy demo --json`, and the canonical `decoy run`
  cell against the bundled minimal template. Manual runbook lives at
  `docs/release/fresh-install-smoke.md`.
- Centralized exit-code contract: `decoy.cli.exit_codes` exports
  `EXIT_OK` (0), `EXIT_USAGE` (1), `EXIT_DEPRECATED_SHIM` (2), and
  `EXIT_RUNTIME` (3). Integer values stable across releases; full table
  at `decoy explain exit-codes`.
- V2-shape bundled examples (`examples/mask_example.yaml` +
  `examples/generate_example.yaml`) with a parametrized validation gate.
- CLI.1-4: rewired `decoy --help`, `decoy run`, `decoy demo`,
  `decoy validate`, `decoy plan` against the V2 engine spine
  (`PipelineConfig.model_validate` -> `compile_plan` ->
  `select_execution_adapter`). Pre-CLI.1 the entire CLI raised
  ImportError at module load against the post-S22 engine.
- FC-1 schema alignment: the top-level `mode:` field is gone (engine
  FC-1 dropped it); `decoy run --mode mask|generate` flag is still
  accepted as a hint, but the actual mode is inferred per-table from
  `columns` (mask) vs `generate_columns` (generate) presence. Bundled
  templates + examples + the demo's emitter all updated.

### Fixed (from QA review docs/qa/review-2026-06-02-cli-v2-migration.md)

- F2: stale `FORECAST` mention in `demo.py` module docstring (the
  legacy recommender was retired under storm-reframe-C).
- F3: `plan.py::_empty_profile_for_no_profile` now uses POSIX epoch as
  a "no real profile" sentinel instead of a hardcoded slice date.
- F5: orphan V1-shape YAML builders in `demo.py` carry per-function
  `# V1 SHAPE -- replace with V2 PipelineConfig before wiring to --ref`
  warnings + a top-of-block fence comment.
- F6: `demo.py::_build_pipeline_yaml` builds a dict and serializes via
  `yaml.safe_dump` instead of f-string templating; a path with a single
  quote (e.g. `--dir "O'Hare_demo"`) no longer crashes.
- F7: `decoy run` + `decoy demo` no longer swallow inner `typer.Exit`
  into the EXIT_RUNTIME catch-all.
- F8: JSON error envelopes truncate engine exception messages at 500
  chars to bound log growth + reduce PII risk.
- F9: `decoy validate` reports "Pipeline YAML is empty." on an empty
  file instead of the unhelpful "must be a YAML mapping, not
  NoneType."
- F10: bare `assert` in `plan.py` replaced with `RuntimeError` so the
  invariant cannot be stripped by `python -O`.
- F11: deleted unused `import os` in `run.py`.
- F12: added "See also:" blocks to `decoy plan --help` and
  `decoy replan --help` epilogs.
- F13: `decoy explain pipeline` topic body leads with the V2 shape;
  the V1 shape is demoted to a "Legacy V1 shape (REJECTED by
  `decoy validate`)" footnote.
- F14: deleted no-op `run.__doc__ = run.__doc__` self-assignment.

### Deferred

- F1 (~150 LOC dispatch dedup between `run.py` and `demo.py`): real
  refactor; should land alongside an `_dispatch.py` extraction.
- F4 (run.py triple YAML read on hot path): real refactor; threads
  the parsed raw dict through `_detect_mode` + `_detect_key_label` +
  the spinner body.

Tracked in `decoy/PLAN.md` so they do not rot.

[Unreleased]: https://github.com/louiskeep/decoy/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/louiskeep/decoy/releases/tag/v0.1.0
