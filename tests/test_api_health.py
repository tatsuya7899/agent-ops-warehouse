"""Tests for GET /health (RAG API phase 3, checkpoint 2).

SPEC-agent-ops-warehouse-rag-api_20260811.md Section 4.4 (unauthenticated
death-check endpoint) / Section 6-8.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


def test_health_returns_200_without_any_authorization_header():
    response = client.get("/health")

    assert response.status_code == 200


def test_health_body_reports_ok_status():
    response = client.get("/health")

    assert response.json()["status"] == "ok"


def test_health_ignores_a_bogus_authorization_header_and_still_succeeds():
    # /health is unauthenticated by design (SPEC Section 4.4) -- an
    # Authorization header, even a wrong one, must not turn it into 401.
    response = client.get("/health", headers={"Authorization": "Bearer not-a-real-token"})

    assert response.status_code == 200
