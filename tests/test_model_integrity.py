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
