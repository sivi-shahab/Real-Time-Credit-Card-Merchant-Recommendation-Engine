"""PROMO-001 — promo eligibility. Re-evaluated at serving time (AC-005)."""
from __future__ import annotations

from datetime import UTC, datetime

from .models import Customer, Merchant, Promotion


def evaluate(
    promo: Promotion,
    customer: Customer,
    merchant: Merchant,
    *,
    now: datetime,
    customer_redemptions: int = 0,
) -> tuple[bool, str]:
    """Returns (eligible, reasonCode). Quota check here does NOT reserve quota —
    reservation is atomic in the redemption service (SDD PROMO-001)."""
    now = now.astimezone(UTC)
    if promo.status != "ACTIVE":
        return False, "PROMO_NOT_ACTIVE"
    if not (promo.startsAt <= now <= promo.endsAt):
        return False, "PROMO_OUT_OF_PERIOD"
    if promo.merchantId != merchant.merchantId:
        return False, "PROMO_MERCHANT_MISMATCH"
    if merchant.status != "ACTIVE":
        return False, "MERCHANT_INACTIVE"
    if promo.eligibleCardTiers and customer.cardTier not in promo.eligibleCardTiers:
        return False, "CARD_TIER_NOT_ELIGIBLE"
    if promo.eligibleCityCodes and customer.cityCode not in promo.eligibleCityCodes:
        return False, "CITY_NOT_ELIGIBLE"
    if promo.campaignQuota and promo.quotaUsed >= promo.campaignQuota:
        return False, "QUOTA_EXHAUSTED"
    if promo.perCustomerLimit and customer_redemptions >= promo.perCustomerLimit:
        return False, "PER_CUSTOMER_LIMIT_REACHED"
    return True, "CARD_PROMO_ELIGIBLE"


def offer_view(promo: Promotion) -> dict:
    """Promo is offered WITH its minimum-spend condition unmet — the system never
    flips that condition to satisfied on its own (PROMO-001)."""
    return {
        "promotionId": promo.promotionId,
        "benefitType": promo.benefitType.value,
        "benefitValue": promo.benefitValue,
        "minSpendMinor": promo.minSpendMinor,
        "maxBenefitMinor": promo.maxBenefitMinor,
        "endsAt": promo.endsAt.isoformat(),
        "conditionStatus": "UNVERIFIED_MIN_SPEND" if promo.minSpendMinor else "NO_MIN_SPEND",
    }
