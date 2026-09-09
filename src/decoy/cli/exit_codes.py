"""Named exit-code constants for the `decoy` CLI surface (OSS.1 / OSS-CODE-4).

The CLI commits to a stable, documented set of process exit codes; before this
module those codes lived as integer literals scattered across nine CLI modules
(`raise typer.Exit(code=1)`, `code=3`, etc.), a public contract held together
only by convention. A single typo would silently change the contract; a
contributor reading the source had no way to learn what each code meant short
of grepping every call site.

The five values below are the public exit-code contract. Their INTEGER VALUES
are stable across releases (callers in shell scripts, CI pipelines, and Make
recipes depend on them). The names may evolve; the integers may not.

OSS.4b (2026-06-02) added EXIT_FINDINGS=4 to support `decoy storm integrity`,
which has a meaningful 4th state distinct from the existing three: the
operation ran cleanly, but the data being checked has issues the integrity
runner flagged. Adding a new constant is a MINOR-eligible additive change per
docs/release/versioning.md (existing integers 0-3 do not move). Source
pattern: semgrep's exit-code split (0 pass, 1 findings, 2 error); the analog
here is 0 clean / 4 findings / 3 runtime crash.

The OOM checker v1 (2026-07-24) added EXIT_CAPACITY=5: `decoy run` and
`decoy preflight` both need a way to say "this job needs more memory than
the host has" distinctly from a generic engine crash (EXIT_RUNTIME) or a bad
config (EXIT_USAGE). `2` is already spoken for (`EXIT_DEPRECATED_SHIM`), so
the new value is `5`, the next unused integer -- 0-4 are pinned and do not
move.

Reference: README "Exit codes" section + `decoy explain exit-codes`.
"""

from __future__ import annotations

EXIT_OK: int = 0
"""Command succeeded. The default exit code for a clean run; matches POSIX
convention. `typer.Exit()` with no argument also exits 0."""

EXIT_USAGE: int = 1
"""Command failed because the user gave bad input: a config that did not pass
validation, a path that did not exist, a flag combination that was invalid.
The fix is in the user's request, not in the CLI itself."""

EXIT_DEPRECATED_SHIM: int = 2
"""The legacy `forge` console script was invoked. The user is on the old name;
the CLI prints a migration hint and exits without running. Reserved for the
`decoy._deprecated` shim only; do not reuse for other deprecation cases
without a public-contract conversation first."""

EXIT_RUNTIME: int = 3
"""The CLI itself blew up at run time: the engine raised an unexpected error,
an output write failed, a temp file vanished mid-run. The fix is in the CLI
or engine, not in the user's request. Distinct from EXIT_USAGE so CI
pipelines can route a runtime crash to a different alert."""

EXIT_FINDINGS: int = 4
"""The CLI ran cleanly but found data issues. Used by data-validation /
integrity verbs (`decoy storm integrity` is the first; future verbs that
audit data rather than execute it will reuse this code). The fix is in the
data being checked, not in the CLI or the user's invocation. Pattern source:
semgrep's exit-code split (0 pass, 1 findings, 2 error). Distinct from
EXIT_USAGE (the user gave bad input) and EXIT_RUNTIME (the CLI crashed)."""

EXIT_CAPACITY: int = 5
"""The job needs more memory than this host (or the budget it was given) has.
Raised by `decoy run` when the engine's out-of-core-FK memory gate refuses a
job, and by `decoy preflight` when its capacity check predicts the same
refusal before the run starts. The fix is a bigger host/cgroup ceiling or a
smaller job -- not a config mistake (EXIT_USAGE) and not an engine defect
(EXIT_RUNTIME).

2026-09-08 (fan-in-only recalibration, paired with the engine's
`fix/ooc-preflight-overreject-recalibration`): capacity refusal is now
FAN-IN-ONLY. The engine's build-floor estimate (the other half of this gate)
is advisory, not a refusal -- a job whose predicted relation-build floor
exceeds its build cap now completes preflight/run with a WARNING (status
"warn", `--fail-on-warning` governs whether that warning itself exits
nonzero), not EXIT_CAPACITY. `out_of_core_insufficient_memory` is kept
recognized as LEGACY-engine compat only (an older engine still running the
pre-recalibration hard-fail gate can still raise it); on a current engine
the only code this exit actually fires for is
`out_of_core_fanin_exceeds_budget` -- when one table has more incoming FK
edges (from distinct child FK columns, which the route DOES allow: the compat
gate only rejects multiple parents into the same child column tuple) than the
budget can seat one co-live DuckDB joiner apiece for, each needing DuckDB's
1 MB minimum. That is a real but uncommon refusal (it takes tens of distinct
FKs into one table at a small budget), not the everyday "job too big for the
host" the build-floor estimate used to raise -- that estimate is now advisory.
v1 checks the out-of-core-FK route only; see `decoy explain exit-codes`."""

__all__ = [
    "EXIT_CAPACITY",
    "EXIT_DEPRECATED_SHIM",
    "EXIT_FINDINGS",
    "EXIT_OK",
    "EXIT_RUNTIME",
    "EXIT_USAGE",
]
