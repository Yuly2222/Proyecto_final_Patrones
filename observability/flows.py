"""Flujos Prefect de observabilidad de AegisVolt.

  pip install -r observability/requirements.txt
  prefect server start                      # UI en http://localhost:4200 (otra terminal)
  python observability/flows.py             # registra el deployment y lo corre cada 5 min
"""
import time

import boto3
import httpx
from prefect import flow, get_run_logger, task
from prefect.artifacts import create_markdown_artifact, create_table_artifact

ENVS = {"dev": 30080, "test": 30081, "prod": 30082}  # NodePort del frontend de cada ambiente
IN_FLIGHT = {"STARTED", "RESERVED", "CHARGED", "DISPATCHED", "COMPENSATING"}
ORPHAN_AFTER_S = 300
dynamodb = boto3.resource("dynamodb", region_name="us-east-1")


def scan(table_name: str) -> list[dict]:
    t, items, kw = dynamodb.Table(table_name), [], {}
    while True:
        page = t.scan(**kw)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            return items
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


@task(retries=2, retry_delay_seconds=5)
def check_health(env: str) -> dict:
    """Salud de la cadena completa: frontend -> gateway -> 3 microservicios -> DynamoDB."""
    url = f"http://localhost:{ENVS[env]}"
    started = time.time()
    try:
        r = httpx.post(f"{url}/graphql", timeout=10, json={
            "query": "{ environment stations { connectorId } wallets { fleetId } telemetry { sessionId } }"})
        ok = r.status_code == 200 and "errors" not in r.json()
    except httpx.HTTPError:
        ok = False
    return {"env": env, "healthy": ok, "latency_ms": int((time.time() - started) * 1000)}


@task
def audit_sagas(env: str) -> dict:
    """Consistencia SAGA en DynamoDB: huérfanas, compensadas, y compensaciones incompletas."""
    log = get_run_logger()
    sagas = scan(f"aegisvolt-sagas-{env}")
    txns = {t["pk"] for t in scan(f"aegisvolt-billing-{env}") if t["pk"].startswith("TXN#")}
    now = time.time()
    counts = {s: 0 for s in ["COMPLETED", "COMPENSATED", "FAILED", "COMPENSATION_FAILED"]}
    orphans, inconsistent = [], []
    for s in sagas:
        status = s["status"]
        if status in IN_FLIGHT and now - float(s.get("updated_at", s["created_at"])) > ORPHAN_AFTER_S:
            orphans.append(s["saga_id"])  # quedó a medias: el gateway murió en medio de la saga
        elif status in counts:
            counts[status] += 1
        # Cobrada y "compensada" pero sin reembolso -> dinero retenido indebidamente
        if status == "COMPENSATED" and f"TXN#{s['saga_id']}#CHARGE" in txns \
                and f"TXN#{s['saga_id']}#REFUND" not in txns:
            inconsistent.append(s["saga_id"])
    for sid in orphans + inconsistent:
        log.warning(f"[{env}] saga con problema: {sid}")
    return {"env": env, "total": len(sagas), **counts, "ORPHANS": len(orphans),
            "INCONSISTENT": len(inconsistent), "problem_ids": ", ".join(orphans + inconsistent)[:200]}


@flow(name="aegisvolt-observability", log_prints=True)
def observability():
    health = [check_health.submit(e) for e in ENVS]
    audits = [audit_sagas.submit(e) for e in ENVS]
    health, audits = [h.result() for h in health], [a.result() for a in audits]

    create_table_artifact(key="aegisvolt-health", table=health, description="Salud por ambiente")
    create_table_artifact(key="aegisvolt-saga-audit", table=audits, description="Auditoría SAGA por ambiente")
    lines = ["| ambiente | sano | sagas | completadas | compensadas | huérfanas | inconsistentes |", "|---|---|---|---|---|---|---|"]
    for h, a in zip(health, audits):
        lines.append(f"| {h['env']} | {'✅' if h['healthy'] else '❌'} | {a['total']} | {a['COMPLETED']} | "
                     f"{a['COMPENSATED']} | {a['ORPHANS']} | {a['INCONSISTENT'] + a['COMPENSATION_FAILED']} |")
    create_markdown_artifact(key="aegisvolt-summary", markdown="\n".join(lines))
    print("\n".join(lines))

    if any(not h["healthy"] for h in health) or any(a["ORPHANS"] or a["INCONSISTENT"] for a in audits):
        raise RuntimeError("Hay ambientes caídos o sagas inconsistentes (ver artefactos)")  # run en rojo en Prefect


if __name__ == "__main__":
    observability.serve(name="cada-5-min", interval=300)
