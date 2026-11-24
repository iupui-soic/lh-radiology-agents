"""/healthz follows the RIS poller (issue #48).

The poller runs as a task in the ingress lifespan. If it dies, sign-off detection stops, so
/healthz must turn unhealthy and the death must be logged with its traceback. The real poller is
swapped for a stand-in so the lifespan runs without a store, fhir2 or Temporal.
"""
from __future__ import annotations

import asyncio
import logging
import time

import pytest

ingress = pytest.importorskip("orchestrator.ingress", reason="orchestrator deps not installed")
from fastapi.testclient import TestClient  # noqa: E402


def _health_when(client, want, timeout=5.0):
    """GET /healthz until the poller reaches `want` (the stand-in runs in the client's loop)."""
    deadline = time.monotonic() + timeout
    while True:
        r = client.get("/healthz")
        if r.json().get("poller") == want or time.monotonic() > deadline:
            return r
        time.sleep(0.02)


def test_healthz_is_ok_while_the_poller_runs(monkeypatch):
    async def runs_forever():
        await asyncio.Event().wait()

    monkeypatch.setattr(ingress, "_ris_poller", runs_forever)
    with TestClient(ingress.app) as client:
        r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "poller": "running"}


def test_a_crashed_poller_turns_healthz_503_and_is_logged_with_its_traceback(monkeypatch, caplog):
    async def crashes():
        raise RuntimeError("ingress store unavailable")

    monkeypatch.setattr(ingress, "_ris_poller", crashes)
    with caplog.at_level(logging.ERROR, logger="orchestrator.ingress"):
        with TestClient(ingress.app) as client:
            r = _health_when(client, "crashed")
    assert r.status_code == 503
    assert r.json() == {"ok": False, "poller": "crashed"}
    logged = caplog.text
    assert "RIS poller crashed" in logged
    assert "Traceback (most recent call last)" in logged
    assert "RuntimeError: ingress store unavailable" in logged


def test_the_logged_traceback_masks_credentials_in_a_url(monkeypatch, caplog):
    async def crashes():
        raise RuntimeError("401 for url 'http://admin:s3cret@fhir2:8080/openmrs/ws/fhir2/R4'")

    monkeypatch.setattr(ingress, "_ris_poller", crashes)
    with caplog.at_level(logging.ERROR, logger="orchestrator.ingress"):
        with TestClient(ingress.app) as client:
            _health_when(client, "crashed")
    assert "http://***@fhir2:8080" in caplog.text
    assert "s3cret" not in caplog.text


def test_shutting_down_cancels_the_poller_without_logging_a_crash(monkeypatch, caplog):
    async def runs_forever():
        await asyncio.Event().wait()

    monkeypatch.setattr(ingress, "_ris_poller", runs_forever)
    with caplog.at_level(logging.ERROR, logger="orchestrator.ingress"):
        with TestClient(ingress.app) as client:
            assert client.get("/healthz").status_code == 200
            poller = ingress.app.state.poller
        assert ingress.app.state.poller is None
        deadline = time.monotonic() + 5
        while not poller.done() and time.monotonic() < deadline:
            time.sleep(0.02)
    assert poller.cancelled()
    assert "RIS poller" not in caplog.text
