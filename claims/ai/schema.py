"""Step 1: the shape of the data the AI pipeline must produce.

Pydantic checks JSON syntax, data types and ranges for us. Missing values are
allowed (they become null) - the pipeline then reports which important fields
are missing instead of crashing.
"""
from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Fields a claim cannot be processed without.
REQUIRED_FIELDS = ("customer_name", "policy_number")


def _blank_to_none(value):
    """Treat '', '  ' and 'N/A'-style answers as missing (null)."""
    if isinstance(value, str):
        value = value.strip()
        if value == "" or value.lower() in {"n/a", "na", "none", "null", "unknown"}:
            return None
    return value


class VehicleDetails(BaseModel):
    model_config = ConfigDict(extra="ignore")

    make: Optional[str] = None
    model: Optional[str] = None
    year: Optional[int] = None

    _clean = field_validator("make", "model", "year", mode="before")(_blank_to_none)

    @field_validator("year")
    @classmethod
    def year_must_be_realistic(cls, v):
        if v is not None and not (1950 <= v <= date.today().year + 1):
            raise ValueError(f"vehicle year {v} is outside 1950-{date.today().year + 1}")
        return v


class ExtractedClaimData(BaseModel):
    """The JSON object described in the project brief."""

    model_config = ConfigDict(extra="ignore")

    customer_name: Optional[str] = None
    policy_number: Optional[str] = None
    accident_description: Optional[str] = None
    vehicle_details: VehicleDetails = Field(default_factory=VehicleDetails)
    damage_description: Optional[str] = None
    images: list[str] = Field(default_factory=list)

    # Extra fields we add for the review step (steps 4 and 6).
    damage_severity: Optional[Literal["minor", "moderate", "severe"]] = None
    confidence: Optional[float] = Field(default=None, ge=0, le=1)

    _clean = field_validator(
        "customer_name",
        "policy_number",
        "accident_description",
        "damage_description",
        "damage_severity",
        mode="before",
    )(_blank_to_none)

    @field_validator("policy_number")
    @classmethod
    def normalise_policy_number(cls, v):
        return v.upper() if v else v

    def missing_required(self) -> list[str]:
        """Names of required fields that are null."""
        return [name for name in REQUIRED_FIELDS if not getattr(self, name)]

    def to_json_dict(self) -> dict:
        return self.model_dump(mode="json")
