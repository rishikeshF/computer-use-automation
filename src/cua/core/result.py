"""What a run returns. Every run ends in exactly one of these; only hard bugs raise.

    Success          the task was done; outputs hold what it read
    BusinessOutcome  the app answered, and the answer is "no" (account_not_found). A result,
                     not an error: the caller's code branches on `code`
    NeedsHuman       paused on purpose (approval, ambiguity); the session is kept for a person
    Failure          the automation could not do its job; `kind` says which way it broke

Recoverable hiccups (a re-login, a retried load) never surface as a status. They are listed in
`evidence.recoveries` so a reviewer can still see them.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ArtifactRef(_Model):
    """Exactly what ran: which artifact, its review status at the time, and its content."""

    id: str  # get_account_balance@0.2.0
    status: str  # draft | approved | deprecated
    content_hash: str


class Evidence(_Model):
    run_id: str
    dir: str  # where the redacted log and screenshots are, relative to the repo
    artifact: ArtifactRef | None = None  # None for discovery, which has no artifact yet
    recoveries: tuple[str, ...] = ()
    interventions: tuple[str, ...] = ()  # what a person approved or did, and who
    raw_outputs: dict[str, str] = {}  # outputs exactly as the screen showed them
    warnings: tuple[str, ...] = ()  # did not change the result, but a person should know


class Success(_Model):
    status: Literal["success"] = "success"
    outputs: dict[str, Decimal | str] = {}  # typed per the artifact: "$515.50" -> 515.50
    evidence: Evidence


class BusinessOutcome(_Model):
    status: Literal["business_outcome"] = "business_outcome"
    code: str
    message: str
    step_id: str | None = None
    evidence: Evidence


class NeedsHuman(_Model):
    status: Literal["needs_human"] = "needs_human"
    reason: str
    intervention_id: str | None = None
    step_id: str | None = None
    evidence: Evidence


FailureKind = Literal[
    "locator_not_found",
    "action_failed",  # the control was found, but clicking or typing into it did not complete
    "checkpoint_mismatch",
    "app_error",
    "policy_violation",
    "timeout",
    "invalid_input",  # the caller's inputs do not fit the artifact's declared inputs
    "precondition_failed",  # e.g. ensure_session could not sign in
    "output_mismatch",  # a value read does not fit its declared type ("N/A" for a decimal)
    # discovery only: the model stopped or could not make progress
    "gave_up",
    "stuck",
    "step_limit",
    "model_error",
]


class Failure(_Model):
    status: Literal["failure"] = "failure"
    kind: FailureKind
    step_id: str | None = None
    expected: str | None = None
    observed: str | None = None
    screenshot: str | None = None  # path inside the evidence dir
    evidence: Evidence


RunResult = Annotated[
    Success | BusinessOutcome | NeedsHuman | Failure, Field(discriminator="status")
]
