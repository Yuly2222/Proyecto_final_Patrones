"""Integración: los 4 servicios reales (en proceso) contra tablas DynamoDB reales (dynamodb-local),
entrando por el endpoint GraphQL. Requiere DYNAMODB_ENDPOINT (p. ej. http://localhost:8000)."""
import os

import pytest
from fastapi.testclient import TestClient

from conftest import load_service

pytestmark = pytest.mark.skipif(not os.getenv("DYNAMODB_ENDPOINT"), reason="requiere dynamodb-local")


@pytest.fixture()
def gql():
    import common
    import local_tables
    from saga import StepFailed

    local_tables.reset()
    common._flags.clear()
    clients = {n: TestClient(load_service(n).app) for n in ("reservation", "billing", "dispatch")}
    gateway = load_service("gateway")

    def call(service, path, payload=None):
        c = clients[service]
        r = c.get(path) if payload is None else c.post(path, json=payload)
        if r.status_code >= 400:
            raise StepFailed(f"{service}{path} -> {r.status_code}")
        return r.json()

    gateway.call = call
    gw = TestClient(gateway.app)

    def run(query):
        body = gw.post("/graphql", json={"query": query}).json()
        assert "errors" not in body, body
        return body["data"]

    run.clients = clients
    return run


SAGA = 'mutation { reserveAndCharge(connectorId: "%s", fleetId: "%s", kwh: 40) { sagaId status } }'


def connector(gql, cid):
    return next(c for c in gql("{ stations { connectorId status } }")["stations"] if c["connectorId"] == cid)


def balance(gql, fleet):
    return next(w["balance"] for w in gql("{ wallets { fleetId balance } }")["wallets"] if w["fleetId"] == fleet)


def test_happy_path(gql):
    saga = gql(SAGA % ("CON-BOG-01-A", "fleet-rapiexpress"))["reserveAndCharge"]
    assert saga["status"] == "COMPLETED"
    assert connector(gql, "CON-BOG-01-A")["status"] == "CHARGING"
    assert balance(gql, "fleet-rapiexpress") == 500 - 40 * 0.35 * 1.5  # surge activo en ci


def test_hardware_fault_flag_triggers_full_compensation(gql):
    gql('mutation { setFeatureFlag(name: "SIMULATE_CHARGER_HARDWARE_FAULT", enabled: true) { enabled } }')
    saga = gql(SAGA % ("CON-BOG-01-B", "fleet-rapiexpress"))["reserveAndCharge"]
    assert saga["status"] == "COMPENSATED"
    assert connector(gql, "CON-BOG-01-B")["status"] == "AVAILABLE"
    assert balance(gql, "fleet-rapiexpress") == 500
    txns = {t["type"] for t in gql.clients["billing"].get("/transactions").json() if t["saga_id"] == saga["sagaId"]}
    assert txns == {"CHARGE", "REFUND"}


def test_insufficient_funds_releases_connector(gql):
    saga = gql(SAGA % ("CON-MED-01-A", "fleet-sinsaldo"))["reserveAndCharge"]
    assert saga["status"] == "COMPENSATED"
    assert connector(gql, "CON-MED-01-A")["status"] == "AVAILABLE"


def test_compensations_are_idempotent(gql):
    gql('mutation { setFeatureFlag(name: "SIMULATE_CHARGER_HARDWARE_FAULT", enabled: true) { enabled } }')
    sid = gql(SAGA % ("CON-LIM-01-A", "fleet-rapiexpress"))["reserveAndCharge"]["sagaId"]
    billing, reservation = gql.clients["billing"], gql.clients["reservation"]
    assert billing.post("/refund", json={"saga_id": sid}).json()["status"] == "ALREADY_REFUNDED"
    assert reservation.post("/release", json={"saga_id": sid, "connector_id": "CON-LIM-01-A"}).json()["status"] \
        == "ALREADY_RELEASED"
    assert balance(gql, "fleet-rapiexpress") == 500  # no se reembolsó dos veces


def test_connector_cannot_be_double_booked(gql):
    gql(SAGA % ("CON-CDMX-01-A", "fleet-rapiexpress"))
    saga = gql(SAGA % ("CON-CDMX-01-A", "fleet-rapiexpress"))["reserveAndCharge"]
    assert saga["status"] == "FAILED"
