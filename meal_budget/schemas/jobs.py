from datetime import datetime
from typing import Literal

from meal_budget.schemas.common import Contract
from meal_budget.schemas.generation import GenerationResult

JobState = Literal["queued", "running", "completed", "failed"]


class JobAccepted(Contract):
    id: str
    state: Literal["queued"] = "queued"


class JobStatus(Contract):
    id: str
    state: JobState
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    catalog_version: str
    result: GenerationResult | None = None
    error_code: str | None = None
