"""ML-003 labels and ML-004 attribution. Pure; used by training and by the live path.

Two rules matter and are easy to get wrong:
  1. An impression is only a negative AFTER the observation window closes. Labelling
     a fresh impression 0 teaches the model that anything recent is bad.
  2. A candidate that was never shown is not a negative. Absence of exposure is not
     evidence of irrelevance.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

ATTRIBUTION_POLICY_VERSION = "last-touch-7d-1.0.0"
ATTRIBUTION_WINDOW = timedelta(days=7)
OBSERVATION_WINDOW = timedelta(days=1)

# ML-003 relevance grades. Highest valid outcome wins.
OUTCOME_LABEL: dict[str, int] = {
    "IMPRESSION": 0,
    "CLICK": 1,
    "PROMO_ACTIVATION": 2,
    "REDEMPTION": 3,
    "PURCHASE": 3,
}


class NotObservable(Exception):
    """The observation window has not closed, so no label can be asserted yet."""


@dataclass(frozen=True)
class Exposure:
    """One item actually shown to a customer — the unit of training."""

    requestId: str
    impressionId: str
    customerId: str
    merchantId: str
    position: int
    shownAt: datetime


@dataclass(frozen=True)
class Outcome:
    impressionId: str
    outcomeType: str
    occurredAt: datetime
    conversionId: str | None = None


def label_exposure(
    exposure: Exposure,
    outcomes: list[Outcome],
    *,
    as_of: datetime,
    attribution_window: timedelta = ATTRIBUTION_WINDOW,
    observation_window: timedelta = OBSERVATION_WINDOW,
) -> int:
    """Relevance grade for one exposure. Raises NotObservable while still open."""
    shown = exposure.shownAt.astimezone(UTC)
    as_of = as_of.astimezone(UTC)
    if as_of < shown + observation_window:
        raise NotObservable(f"{exposure.impressionId} observable at {shown + observation_window}")

    label = 0
    for outcome in outcomes:
        if outcome.impressionId != exposure.impressionId:
            continue
        occurred = outcome.occurredAt.astimezone(UTC)
        if not (shown <= occurred <= shown + attribution_window):
            continue  # outside the attribution window: not this exposure's credit
        label = max(label, OUTCOME_LABEL.get(outcome.outcomeType, 0))
    return label


def attribute_conversion(
    conversion: Outcome,
    exposures: list[Exposure],
    *,
    attribution_window: timedelta = ATTRIBUTION_WINDOW,
) -> Exposure | None:
    """Last-touch within the window: one conversion gets exactly one credited exposure.

    Last-touch is adequate for reporting. Causal uplift needs a controlled experiment,
    which this function deliberately does not pretend to provide (ML-004).
    """
    occurred = conversion.occurredAt.astimezone(UTC)
    eligible = [
        e for e in exposures
        if e.impressionId == conversion.impressionId
        and e.shownAt.astimezone(UTC) <= occurred <= e.shownAt.astimezone(UTC) + attribution_window
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda e: e.shownAt)


def policy_metadata() -> dict:
    return {
        "attributionPolicyVersion": ATTRIBUTION_POLICY_VERSION,
        "attributionWindowDays": ATTRIBUTION_WINDOW.days,
        "observationWindowDays": OBSERVATION_WINDOW.days,
        "rule": "last-touch",
        "labelMap": dict(OUTCOME_LABEL),
    }


def label_from_events(
    events: list[dict],
    *,
    as_of: datetime,
    attribution_window: timedelta = ATTRIBUTION_WINDOW,
    observation_window: timedelta = OBSERVATION_WINDOW,
) -> dict[str, int]:
    """Collapse a feedback stream into impressionId -> label.

    Only impressions whose observation window has closed appear in the result; the
    rest are withheld rather than labelled as negatives.
    """
    exposures: dict[str, Exposure] = {}
    # Indexed by impressionId: scanning every outcome per exposure is O(n*m) and
    # dominates dataset build time on any realistic feedback volume.
    outcomes: dict[str, list[Outcome]] = {}
    for event in events:
        occurred = _ts(event["occurredAt"])
        if event["eventType"] == "IMPRESSION":
            exposures[event["impressionId"]] = Exposure(
                requestId=event["requestId"], impressionId=event["impressionId"],
                customerId=event["customerId"], merchantId=event["merchantId"],
                position=int(event.get("position", 0)), shownAt=occurred)
        else:
            outcomes.setdefault(event["impressionId"], []).append(Outcome(
                impressionId=event["impressionId"], outcomeType=event["eventType"],
                occurredAt=occurred,
                conversionId=event.get("conversionId") or event["impressionId"]))

    labels: dict[str, int] = {}
    for impression_id, exposure in exposures.items():
        try:
            labels[impression_id] = label_exposure(
                exposure, outcomes.get(impression_id, []), as_of=as_of,
                attribution_window=attribution_window,
                observation_window=observation_window)
        except NotObservable:
            continue
    return labels


def _ts(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
