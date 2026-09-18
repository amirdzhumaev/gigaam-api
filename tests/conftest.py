import os

import pytest
from fastapi.testclient import TestClient

from gigaam_api.app import Settings, create_app

A = "test-owner-a-" + "a" * 24
B = "test-owner-b-" + "b" * 24
W = "test-worker-" + "w" * 24


@pytest.fixture
def api(tmp_path):
    # TEST_DATABASE_URL must point at a disposable test database; never production.
    url = os.environ.get("TEST_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    app = create_app(Settings(url, tmp_path, {A: "alice", B: "bob"}, W, 1024))
    with app.state.engine.begin() as db:
        db.execute(app.state.jobs.delete())
    with TestClient(app, headers={"Authorization": f"Bearer {A}"}) as client:
        yield client
    app.state.engine.dispose()


@pytest.fixture
def worker(api):
    with TestClient(api.app, headers={"Authorization": f"Bearer {W}"}) as client:
        yield client
