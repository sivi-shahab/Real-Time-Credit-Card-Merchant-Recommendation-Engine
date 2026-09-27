"""Threat T-4: the ranking service loads only the artifact training recorded."""
import hashlib

import numpy as np
import pytest
import xgboost as xgb
from fastapi.testclient import TestClient

from rec.ml.vectorize import FEATURE_NAMES
from rec.ranking import service
from rec.settings import settings


@pytest.fixture
def model(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "model_dir", str(tmp_path))
    monkeypatch.setattr(service, "registry", service._Registry())
    rows = np.random.default_rng(0).random((20, len(FEATURE_NAMES)), dtype=np.float32)
    booster = xgb.train({"objective": "binary:logistic"},
                        xgb.DMatrix(rows, label=np.arange(20) % 2,
                                    feature_names=list(FEATURE_NAMES)), num_boost_round=2)
    path = tmp_path / "m-1.json"
    booster.save_model(path)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _score(client, digest):
    return client.post("/v1/score", json={
        "modelVersion": "m-1", "modelSha256": digest,
        "candidates": [{name: 0.5 for name in FEATURE_NAMES}]})


def test_artifact_replaced_after_training_is_refused(model):
    path, digest = model
    client = TestClient(service.app)
    other = "0" * 64

    assert _score(client, other).status_code == 412, "wrong digest must not load"
    assert client.post("/v1/models/m-1/warm", json={"modelSha256": other}).status_code == 412
    assert _score(client, None).status_code == 422, "a digest is required"

    path.write_bytes(path.read_bytes().replace(b"binary:logistic", b"binary:logitraw"))
    assert _score(client, digest).status_code == 412, "tampered file must not load"

    path.write_bytes(path.read_bytes().replace(b"binary:logitraw", b"binary:logistic"))
    assert client.post("/v1/models/m-1/warm", json={"modelSha256": digest}).status_code == 200
    assert _score(client, digest).status_code == 200
    # once loaded, a request carrying another digest is still refused
    assert _score(client, other).status_code == 412


def _train_into(path):
    rows = np.random.default_rng(0).random((20, len(FEATURE_NAMES)), dtype=np.float32)
    xgb.train({"objective": "binary:logistic"},
              xgb.DMatrix(rows, label=np.arange(20) % 2, feature_names=list(FEATURE_NAMES)),
              num_boost_round=2).save_model(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_only_the_api_reaches_the_ranking_service_and_memory_is_bounded(tmp_path,
                                                                       monkeypatch):
    """Threat D-5: a service token on /v1, and at most `ranking_max_models` boosters."""
    monkeypatch.setattr(settings, "model_dir", str(tmp_path))
    monkeypatch.setattr(settings, "ranking_max_models", 2)
    monkeypatch.setattr(service, "registry", service._Registry())
    digests = {f"m-{i}": _train_into(tmp_path / f"m-{i}.json") for i in range(3)}
    client = TestClient(service.app)

    def warm(version, headers=None):
        return client.post(f"/v1/models/{version}/warm", headers=headers or {},
                           json={"modelSha256": digests[version]}).status_code

    monkeypatch.setattr(settings, "environment", "production")
    assert warm("m-0") == 503, "no token configured outside dev: closed"
    monkeypatch.setattr(settings, "ranking_service_token", "s3cret")
    assert warm("m-0") == 401
    assert warm("m-0", {"authorization": "Bearer wrong"}) == 401
    ok = {"authorization": "Bearer s3cret"}
    assert client.get("/health").status_code == 200  # probes stay open

    for version in ("m-0", "m-1", "m-0", "m-2"):   # m-0 used again, so m-1 is the LRU
        assert warm(version, ok) == 200
    assert service.registry.loaded() == ["m-0", "m-2"]
    assert warm("m-1", ok) == 200, "an evicted model reloads on its next use"
