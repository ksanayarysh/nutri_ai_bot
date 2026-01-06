from dataclasses import dataclass


@dataclass
class Macros:
    name: str
    qty: float
    unit: str
    calories: float | None
    protein: float | None
    fat: float | None
    carbs: float | None
    fiber: float | None

