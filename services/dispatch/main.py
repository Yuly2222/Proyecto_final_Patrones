"""energy-dispatch-service: activación física del cargador y telemetría (tabla aegisvolt-dispatch-<env>)."""
import logging
import time

from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from common import ENV, flag, log, table

app = FastAPI(title="energy-dispatch-service")
sessions = table("dispatch")


class StartReq(BaseModel):
    saga_id: str
    connector_id: str


@app.get("/health")
def health():
    return {"status": "ok", "env": ENV}


@app.get("/sessions")
def list_sessions():
    """Telemetría simulada: kWh entregados = potencia * tiempo transcurrido."""
    now = time.time()
    out = []
    for s in sessions.scan()["Items"]:
        if s["status"] == "ACTIVE":
            s["kwh_delivered"] = round(float(s["power_kw"]) * (now - float(s["started_at"])) / 3600, 3)
        out.append(s)
    return out


@app.post("/start")
def start(r: StartReq):
    if flag("SIMULATE_CHARGER_HARDWARE_FAULT"):
        # Falla de negocio esperada -> WARNING (no ERROR) para no tumbar el canary cuando la flag está activa.
        sessions.put_item(Item={"session_id": r.saga_id, "connector_id": r.connector_id, "status": "FAILED",
                                "reason": "HARDWARE_FAULT", "started_at": int(time.time()), "power_kw": 0})
        log("charger_hardware_fault", r.saga_id, logging.WARNING, connector_id=r.connector_id)
        raise HTTPException(503, "charger hardware fault")
    power = 350 if flag("ENABLE_FAST_CHARGE_BOOST") else 150
    try:
        sessions.put_item(
            Item={"session_id": r.saga_id, "connector_id": r.connector_id, "status": "ACTIVE",
                  "power_kw": power, "started_at": int(time.time())},
            ConditionExpression="attribute_not_exists(session_id)",  # reintento idempotente
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    log("charging_started", r.saga_id, connector_id=r.connector_id, power_kw=power)
    return {"status": "ACTIVE", "power_kw": power}
