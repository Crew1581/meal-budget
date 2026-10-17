"""Ограниченная диагностика локальной оценки, не HTTP-контракт генерации."""

from typing import Annotated, Literal

from pydantic import Field

from meal_budget.schemas.common import Contract

MAX_ATTEMPT_VIOLATIONS = 20
DiagnosticStage = Literal["parsing", "schema", "calculation", "constraints"]
DiagnosticNumber = Annotated[
    str, Field(max_length=120, pattern=r"^-?\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?$")
]


class DiagnosticViolation(Contract):
    code: str = Field(max_length=64, pattern=r"^[a-z_]+$")
    field: str = Field(max_length=200)
    actual: DiagnosticNumber | None = None
    minimum: DiagnosticNumber | None = None
    maximum: DiagnosticNumber | None = None


class AttemptDiagnostic(Contract):
    attempt: int = Field(ge=1, le=3)
    stage: DiagnosticStage
    available: bool = True
    violations: list[DiagnosticViolation] = Field(max_length=MAX_ATTEMPT_VIOLATIONS)
    violation_count: int | None = Field(ge=0)
    truncated: bool | None
