"""SYN-001 — generator configuration. Same config + seed + version => same checksums."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

GENERATOR_VERSION = "1.0.0"

CATEGORIES = [
    "F&B", "GROCERY", "FASHION", "ELECTRONICS", "TRAVEL",
    "FUEL", "HEALTH", "ENTERTAINMENT", "EDUCATION", "HOME",
]
CITIES = ["JKT", "BDG", "SBY", "MDN", "MKS", "SMG", "DPS", "PLM"]
CARD_TIERS = ["CLASSIC", "GOLD", "PLATINUM", "INFINITE"]

# median ticket per category, IDR minor units
CATEGORY_TICKET_MINOR = {
    "F&B": 85_000, "GROCERY": 220_000, "FASHION": 450_000, "ELECTRONICS": 2_500_000,
    "TRAVEL": 3_200_000, "FUEL": 200_000, "HEALTH": 400_000,
    "ENTERTAINMENT": 150_000, "EDUCATION": 1_200_000, "HOME": 700_000,
}


class FailureInjection(BaseModel):
    """SYN-005 — every rate is a fraction of generated transactions."""

    duplicateEventRate: float = Field(0.01, ge=0, le=0.5)
    duplicateTransactionRate: float = Field(0.005, ge=0, le=0.5)
    zeroAmountRate: float = Field(0.002, ge=0, le=0.5)
    excessiveRefundRate: float = Field(0.002, ge=0, le=0.5)
    unknownCustomerRate: float = Field(0.001, ge=0, le=0.5)
    unknownMerchantRate: float = Field(0.001, ge=0, le=0.5)
    futureTimestampRate: float = Field(0.001, ge=0, le=0.5)
    lateEventRate: float = Field(0.02, ge=0, le=0.5)


class DatasetConfig(BaseModel):
    """SYN-001."""

    seed: int = 42
    # SIM-002: running a dataset as an independent experiment needs its own ID
    # namespace, otherwise a second replay accumulates onto the first one's state.
    idNamespace: str = ""
    referenceTime: datetime = datetime(2026, 9, 23, tzinfo=UTC)
    customerCount: int = 2_000
    merchantCount: int = 300
    promotionCount: int = 60
    transactionCount: int = 50_000
    historyDays: int = 180
    currency: Literal["IDR"] = "IDR"
    refundRate: float = Field(0.03, ge=0, le=0.5)
    reversalRate: float = Field(0.01, ge=0, le=0.5)
    driftCustomerShare: float = Field(0.1, ge=0, le=1)
    newCustomerShare: float = Field(0.08, ge=0, le=1)
    noPersonalizationShare: float = Field(0.05, ge=0, le=1)
    scenarioProfile: Literal["normal", "mixed", "failure"] = "mixed"
    failureInjection: FailureInjection = FailureInjection()
    outputFormats: list[Literal["parquet", "jsonl"]] = ["parquet", "jsonl"]
    generatorVersion: str = GENERATOR_VERSION

    def effective_failures(self) -> FailureInjection:
        if self.scenarioProfile == "normal":
            return FailureInjection(**{k: 0.0 for k in FailureInjection.model_fields})
        if self.scenarioProfile == "failure":
            return FailureInjection(
                **{k: min(v * 5, 0.5) for k, v in self.failureInjection.model_dump().items()}
            )
        return self.failureInjection
