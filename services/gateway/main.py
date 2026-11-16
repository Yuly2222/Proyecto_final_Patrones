"""GraphQL Aggregation Layer: esquema único sobre los 3 microservicios + orquestador SAGA."""
import os

import httpx
import strawberry
from fastapi import FastAPI
from strawberry.fastapi import GraphQLRouter
from strawberry.scalars import JSON

from common import ENV, table
from common import _flags as _flag_cache
from saga import StepFailed, run_saga

URLS = {  # nombres DNS de los Services de Kubernetes (o de docker-compose)
    "reservation": os.getenv("RESERVATION_URL", "http://reservation:8000"),
    "billing": os.getenv("BILLING_URL", "http://billing:8000"),
    "dispatch": os.getenv("DISPATCH_URL", "http://dispatch:8000"),
}
FLAGS = ["ENABLE_DYNAMIC_SURGE_PRICING", "ENABLE_FAST_CHARGE_BOOST", "SIMULATE_CHARGER_HARDWARE_FAULT"]


def http_call(service: str, path: str, payload: dict | None = None):
    """Llamada a un microservicio. Errores 4xx/5xx o de red -> StepFailed (lo maneja la SAGA)."""
    try:
        if payload is None:
            r = httpx.get(URLS[service] + path, timeout=5)
        else:
            r = httpx.post(URLS[service] + path, json=payload, timeout=5)
    except httpx.HTTPError as e:
        raise StepFailed(f"{service} unreachable: {e}")
    if r.status_code >= 400:
        raise StepFailed(f"{service}{path} -> {r.status_code} {r.text}")
    return r.json()


call = http_call  # los tests de integración lo reemplazan para enrutar a TestClients


@strawberry.type
class Connector:
    connector_id: str
    station_name: str
    power_kw: int
    status: str
    saga_id: str | None


@strawberry.type
class FeatureFlag:
    name: str
    enabled: bool
    env: str


@strawberry.type
class Saga:
    saga_id: str
    status: str
    connector_id: str
    fleet_id: str
    amount: str | None
    steps: JSON
    created_at: int


@strawberry.type
class Wallet:
    fleet_id: str
    balance: float


@strawberry.type
class ChargingSession:
    session_id: str
    connector_id: str
    status: str
    power_kw: int
    kwh_delivered: float | None


def _saga(i: dict) -> Saga:
    return Saga(saga_id=i["saga_id"], status=i["status"], connector_id=i["connector_id"], fleet_id=i["fleet_id"],
                amount=i.get("amount"), steps=[{**s, "at": int(s["at"])} for s in i.get("steps", [])],
                created_at=int(i["created_at"]))


@strawberry.type
class Query:
    @strawberry.field
    def environment(self) -> str:
        return ENV

    @strawberry.field
    def stations(self) -> list[Connector]:
        return [Connector(connector_id=c["connector_id"], station_name=c.get("station_name", ""),
                          power_kw=int(c.get("power_kw", 0)), status=c["status"], saga_id=c.get("saga_id"))
                for c in call("reservation", "/connectors")]

    @strawberry.field
    def feature_flags(self) -> list[FeatureFlag]:
        flags = table("flags")
        return [FeatureFlag(name=n, enabled=bool((flags.get_item(Key={"flag": n}).get("Item") or {}).get("enabled")),
                            env=ENV) for n in FLAGS]

    @strawberry.field
    def sagas(self) -> list[Saga]:
        items = table("sagas").scan()["Items"]
        return [_saga(i) for i in sorted(items, key=lambda i: -int(i["created_at"]))]

    @strawberry.field
    def wallets(self) -> list[Wallet]:
        return [Wallet(fleet_id=w["fleet_id"], balance=float(w["balance"])) for w in call("billing", "/wallets")]

    @strawberry.field
    def telemetry(self) -> list[ChargingSession]:
        return [ChargingSession(session_id=s["session_id"], connector_id=s["connector_id"], status=s["status"],
                                power_kw=int(s["power_kw"]), kwh_delivered=s.get("kwh_delivered"))
                for s in call("dispatch", "/sessions")]


@strawberry.type
class Mutation:
    @strawberry.mutation
    def reserve_and_charge(self, connector_id: str, fleet_id: str, kwh: float) -> Saga:
        """Dispara la SAGA: reservar -> cobrar -> activar cargador (con compensaciones si algo falla)."""
        return _saga(run_saga(call, connector_id, fleet_id, kwh))

    @strawberry.mutation
    def release_connector(self, saga_id: str, connector_id: str) -> str:
        """Termina una carga (CHARGING -> AVAILABLE) para volver a usar el conector en la demo."""
        return call("reservation", "/release", {"saga_id": saga_id, "connector_id": connector_id})["status"]

    @strawberry.mutation
    def set_feature_flag(self, name: str, enabled: bool) -> FeatureFlag:
        """Cambia la flag SOLO en la tabla de este ambiente (aegisvolt-flags-<env>): sin redespliegue."""
        table("flags").update_item(Key={"flag": name}, UpdateExpression="SET enabled = :e",
                                   ExpressionAttributeValues={":e": enabled})
        _flag_cache.pop(name, None)  # los otros pods lo verán en <= 3 s (TTL de caché en common.flag)
        return FeatureFlag(name=name, enabled=enabled, env=ENV)


schema = strawberry.Schema(query=Query, mutation=Mutation)
app = FastAPI(title="graphql-gateway")
app.include_router(GraphQLRouter(schema), prefix="/graphql")


@app.get("/health")
def health():
    return {"status": "ok", "env": ENV}
