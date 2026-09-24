"""Domain contracts. Money is ALWAYS integer minor units (IDR: 150000 == Rp150.000)."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

EVENT_VERSION = "1.0.0"
FEATURE_SCHEMA_VERSION = "1.0.0"
RANKING_CONFIG_VERSION = "baseline-1.0.0"


class TxnType(StrEnum):
    PURCHASE = "PURCHASE"
    REFUND = "REFUND"
    REVERSAL = "REVERSAL"


class Channel(StrEnum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"


class CardTier(StrEnum):
    CLASSIC = "CLASSIC"
    GOLD = "GOLD"
    PLATINUM = "PLATINUM"
    INFINITE = "INFINITE"


class BenefitType(StrEnum):
    CASHBACK = "CASHBACK"
    DISCOUNT = "DISCOUNT"
    POINTS = "POINTS"
    INSTALLMENT = "INSTALLMENT"


class TransactionPayload(BaseModel):
    """EVT-001 typed payload. No PAN/CVV/name: SYN-002."""

    transactionId: str
    customerId: str
    merchantId: str
    transactionType: TxnType
    amountMinor: int
    currency: str = "IDR"
    occurredAt: datetime
    originalTransactionId: str | None = None
    categoryCode: str | None = None
    cityCode: str | None = None
    channel: Channel | None = None

    @field_validator("amountMinor")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("amountMinor must be > 0 (SYN-005: zero/negative rejected)")
        return v


class Envelope(BaseModel):
    """EVT-001 standard envelope."""

    eventId: str
    eventType: str
    eventVersion: str = EVENT_VERSION
    occurredAt: datetime
    producedAt: datetime | None = None
    producer: str = "unknown"
    traceparent: str | None = None
    correlationId: str | None = None
    payload: dict[str, Any]

    def transaction(self) -> TransactionPayload:
        return TransactionPayload.model_validate(self.payload)


class Merchant(BaseModel):
    merchantId: str
    merchantName: str
    categoryCode: str
    cityCode: str
    channel: Channel
    rating: float = Field(ge=0, le=5)
    status: Literal["ACTIVE", "INACTIVE"] = "ACTIVE"


class Promotion(BaseModel):
    promotionId: str
    merchantId: str
    benefitType: BenefitType
    benefitValue: float
    minSpendMinor: int = 0
    maxBenefitMinor: int | None = None
    eligibleCardTiers: list[CardTier] = []
    eligibleCityCodes: list[str] = []
    startsAt: datetime
    endsAt: datetime
    campaignQuota: int = 0
    quotaUsed: int = 0
    perCustomerLimit: int = 1
    status: Literal["DRAFT", "ACTIVE", "PAUSED", "EXPIRED"] = "ACTIVE"


class Customer(BaseModel):
    customerId: str
    cityCode: str
    cardTier: CardTier
    personalizationAllowed: bool = True


class Recommendation(BaseModel):
    merchantId: str
    merchantName: str
    categoryCode: str
    rank: int
    score: float
    scoreType: Literal["RELATIVE_RELEVANCE"] = "RELATIVE_RELEVANCE"
    reasonCodes: list[str] = []
    promotion: dict[str, Any] | None = None


class RecommendationResponse(BaseModel):
    """SERV-002."""

    requestId: str
    customerId: str
    generatedAt: datetime
    featureAsOf: datetime | None = None
    modelVersion: str
    featureSchemaVersion: str = FEATURE_SCHEMA_VERSION
    rankingConfigVersion: str = RANKING_CONFIG_VERSION
    source: Literal["CACHE", "LIVE", "FALLBACK"]
    stale: bool = False
    recommendations: list[Recommendation]
