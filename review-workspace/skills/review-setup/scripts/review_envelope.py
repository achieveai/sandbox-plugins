"""The machine-readable result envelope shared by review_setup.py and review_reset.py.

The daemon dispatches a script invocation under an identity (``--run-id``) and reads back
exactly one envelope object on stdout. Under ``--json`` nothing else may reach stdout, on
any path, including errors -- an unparseable stream is indistinguishable from a crash.

The schema and the five statuses are declared in ``bundle.json`` (``resultEnvelope``) and
must stay in step with it; ``test_envelope.py`` pins that.

Argument-parsing failures are the one exception: they exit before an operation exists to
report on, and every CLI-misuse case in these scripts already exits through
``parser.error``.
"""
from __future__ import annotations

import json
import re

ENVELOPE_SCHEMA_VERSION = 1
STATUSES = ("completed", "no_op", "retryable", "blocked", "failed")

# Same grammar as SourcePool._validate_run_id, so an id the daemon can dispatch under is an
# id the script can also carry into a Git ref namespace.
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

# The exact message ReviewState.lock() raises when another process holds the allocation
# lock. Contention is transient, so it maps to `retryable` rather than `failed`.
LOCK_CONTENTION_MESSAGE = "Another review setup/reset operation is running."


def add_arguments(parser) -> None:
    """Add the envelope flags every daemon-facing CLI carries."""
    parser.add_argument(
        "--json", action="store_true",
        help="Emit exactly one result-envelope object on stdout and nothing else.",
    )
    parser.add_argument(
        "--run-id",
        help="Caller's run identity, echoed verbatim as the envelope's runId. Required with --json.",
    )


def validate_arguments(parser, args) -> None:
    """Reject an unusable identity before any work starts."""
    if args.run_id is not None and not RUN_ID_PATTERN.fullmatch(args.run_id):
        parser.error(
            "--run-id must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}"
        )
    if args.json and args.run_id is None:
        parser.error("--json requires --run-id")


class Envelope:
    """Accumulates the artifacts and effects of one invocation, then prints it once."""

    def __init__(self, operation: str, run_id: str | None):
        self.operation = operation
        self.run_id = run_id
        self.artifacts: list[dict] = []
        self.effects: list[dict] = []

    def effect(self, kind: str, path: str = "", detail: str = "", **extra) -> None:
        """Record one structured effect. Extra keys carry the operation's detail fields."""
        self.effects.append({"type": kind, "path": path, "detail": detail, **extra})

    def artifact(self, path: str, kind: str, sha256: str | None = None) -> None:
        entry = {"path": path, "kind": kind}
        if sha256 is not None:
            entry["sha256"] = sha256
        self.artifacts.append(entry)

    def build(self, status: str) -> dict:
        if status not in STATUSES:
            raise ValueError(f"Unknown envelope status {status!r}.")
        return {
            "schemaVersion": ENVELOPE_SCHEMA_VERSION,
            "status": status,
            "runId": self.run_id,
            "operation": self.operation,
            "artifacts": self.artifacts,
            "effects": self.effects,
        }

    def render(self, status: str) -> str:
        """One line, so a reader that takes the last stdout line and a reader that parses
        the whole stream both see the same object."""
        return json.dumps(self.build(status), sort_keys=True)
