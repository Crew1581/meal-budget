from typing import Literal

from pydantic import Field, model_validator

from meal_budget.schemas.common import Contract, Money, ProductId, ShortText
from meal_budget.schemas.diagnostics import AttemptDiagnostic, ResponseMetadata
from meal_budget.schemas.generation import GeneratedPlan, GenerationMetadata, GenerationRequest


class EvaluationScenario(Contract):
    id: ProductId
    title: ShortText
    status: Literal["blocked", "ready"]
    expected: Literal["success", "unsatisfiable"]
    preparation: str = Field(min_length=1, max_length=1000)
    catalog_version: ShortText | None = None
    request: GenerationRequest | None
    witness: GeneratedPlan | None
    minimum_purchase: Money | None = None

    @model_validator(mode="after")
    def readiness(self) -> "EvaluationScenario":
        if self.status == "blocked":
            if self.request is not None or self.witness is not None or self.minimum_purchase:
                raise ValueError("blocked scenarios must not contain unverified questionnaires")
        elif self.request is None:
            raise ValueError("ready scenario requires a questionnaire")
        elif self.catalog_version is None:
            raise ValueError("ready scenario requires a catalog version")
        elif self.expected == "success" and self.witness is None:
            raise ValueError("success scenario requires a feasibility witness")
        elif self.expected == "unsatisfiable" and (
            self.minimum_purchase is None or self.request.budget >= self.minimum_purchase
        ):
            raise ValueError("unsatisfiable scenario requires a strict purchase lower bound")
        return self


class ManualAssessment(Contract):
    portions: str | None = None
    recipe_clarity: str | None = None
    unlisted_ingredients: str | None = None
    variety_and_repeats: str | None = None
    cooking_convenience: str | None = None
    package_leftovers: str | None = None
    conclusion: str | None = None


class RunSummary(Contract):
    scenario: ProductId
    catalog_version: ShortText
    requested_model: str | None = None
    thinking: Literal["disabled", "enabled", "omit"] | None = None
    outcome: Literal["success", "error", "blocked", "not_run"]
    error_code: str | None = None
    metadata: GenerationMetadata | None = None
    # Отдельно от общих с HTTP метаданных; None у старых отчетов.
    response_metadata: list[ResponseMetadata] | None = Field(default=None, max_length=3)
    # None у старых отчетов: история причин тогда не собиралась.
    attempts: list[AttemptDiagnostic] | None = Field(default=None, max_length=3)
    budget_pass: bool | None = None
    nutrition_pass: bool | None = None
    manual: ManualAssessment = Field(default_factory=ManualAssessment)
