from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

# float используется только для JSON-числа на выходе, никогда для расчётов.
Money = Annotated[
    Decimal,
    Field(gt=0, le=1_000_000, max_digits=9, decimal_places=2, allow_inf_nan=False),
    PlainSerializer(lambda value: float(value), return_type=float, when_used="json"),
]
JSONNumber = Annotated[
    Decimal,
    PlainSerializer(float, return_type=float, when_used="json"),
]
PositiveNumber = Annotated[JSONNumber, Field(gt=0, le=100_000, allow_inf_nan=False)]
ProductId = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")]
ShortText = Annotated[str, Field(min_length=1, max_length=120)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Nutrition(Contract):
    calories_kcal: PositiveNumber
    protein_g: Annotated[JSONNumber, Field(ge=0, le=10_000, allow_inf_nan=False)]
    fat_g: Annotated[JSONNumber, Field(ge=0, le=10_000, allow_inf_nan=False)]
    carbs_g: Annotated[JSONNumber, Field(ge=0, le=10_000, allow_inf_nan=False)]
