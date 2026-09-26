"""Continuous learning stage 2: the online bandit's math and its JSON state."""
from __future__ import annotations

import random

import pytest

from rec.ml import bandit
from rec.ml.vectorize import FEATURE_NAMES


def test_state_round_trips_through_json_and_keeps_learning_identically():
    rng = random.Random(3)
    xs = [{f: rng.random() for f in FEATURE_NAMES} for _ in range(200)]
    model = bandit.new_model()
    for x in xs:
        model.learn_one(x, rng.choice([0, 1, 2, 3]))

    restored = bandit.load(bandit.dump(model))
    assert bandit.ucb(restored, xs[:20], 1.0) == pytest.approx(bandit.ucb(model, xs[:20], 1.0))

    model.learn_one(xs[0], 3)
    restored.learn_one(xs[0], 3)
    assert bandit.ucb(restored, xs[:20], 1.0) == pytest.approx(bandit.ucb(model, xs[:20], 1.0))


def test_it_learns_to_rank_the_rewarded_context_first():
    rewarded, ignored = {"a": 1.0, "b": 0.0}, {"a": 0.0, "b": 1.0}
    model = bandit.new_model()
    for _ in range(50):
        model.learn_one(rewarded, 3)
        model.learn_one(ignored, 0)
    good, bad = bandit.ucb(model, [rewarded, ignored], exploration=1.0)
    assert good > bad


def test_exploration_lifts_a_context_it_has_rarely_seen():
    seen, rare = {"a": 1.0}, {"b": 1.0}
    model = bandit.new_model()
    for _ in range(200):
        model.learn_one(seen, 1)
    exploit_seen, exploit_rare = bandit.ucb(model, [seen, rare], exploration=0.0)
    explore_seen, explore_rare = bandit.ucb(model, [seen, rare], exploration=5.0)
    assert exploit_seen > exploit_rare
    assert explore_rare > explore_seen
