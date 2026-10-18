from typing import Literal
from uuid import UUID

from meal_budget.schemas.common import Contract


class InstallationCreated(Contract):
    installation_id: UUID
    token: str
    token_type: Literal["Bearer"] = "Bearer"
