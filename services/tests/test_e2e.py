"""E2E: el stack completo levantado (docker compose o Kubernetes), entrando por el frontend (nginx -> /graphql).
Requiere E2E_URL (p. ej. http://localhost:8080)."""
import os
import time

import httpx
import pytest

URL = os.getenv("E2E_URL")
pytestmark = pytest.mark.skipif(not URL, reason="requiere E2E_URL")


def gql(query):
    body = httpx.post(f"{URL}/graphql", json={"query": query}, timeout=15).json()
    assert "errors" not in body, body
    return body["data"]


def set_fault(on: bool):
    gql('mutation { setFeatureFlag(name: "SIMULATE_CHARGER_HARDWARE_FAULT", enabled: %s) { enabled } }'
        % str(on).lower())
    time.sleep(3.5)  # TTL de la caché de flags en los otros pods


def free_connector():
    return next(c["connectorId"] for c in gql("{ stations { connectorId status } }")["stations"]
                if c["status"] == "AVAILABLE")


def test_frontend_is_served():
    assert "AegisVolt" in httpx.get(URL, timeout=10).text


def test_full_flow_success_and_rollback():
    cid = free_connector()
    ok = gql('mutation { reserveAndCharge(connectorId: "%s", fleetId: "fleet-rapiexpress", kwh: 10) '
             '{ sagaId status } }' % cid)["reserveAndCharge"]
    assert ok["status"] == "COMPLETED"
    gql('mutation { releaseConnector(sagaId: "%s", connectorId: "%s") }' % (ok["sagaId"], cid))

    set_fault(True)
    try:
        bad = gql('mutation { reserveAndCharge(connectorId: "%s", fleetId: "fleet-rapiexpress", kwh: 10) '
                  '{ sagaId status } }' % cid)["reserveAndCharge"]
        assert bad["status"] == "COMPENSATED"
        assert any(s["sagaId"] == bad["sagaId"] and s["status"] == "COMPENSATED"
                   for s in gql("{ sagas { sagaId status } }")["sagas"])
    finally:
        set_fault(False)
