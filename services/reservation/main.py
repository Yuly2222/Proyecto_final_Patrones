"""station-reservation-service: disponibilidad y bloqueo temporal de conectores (tabla aegisvolt-reservations-<env>)."""
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from common import ENV, log, table, setup_tracing

app = FastAPI(title="station-reservation-service")
setup_tracing(app)
connectors = table("reservations")


class Req(BaseModel):
    saga_id: str
    connector_id: str


@app.get("/health")
def health():
    return {"status": "ok", "env": ENV}


@app.get("/connectors")
def list_connectors():
    return connectors.scan()["Items"]


@app.post("/reserve")
def reserve(r: Req):
    # Condicional: solo si está AVAILABLE, o si ya la reservó ESTA misma saga (reintento idempotente).
    try:
        connectors.update_item(
            Key={"connector_id": r.connector_id},
            UpdateExpression="SET #s = :reserved, saga_id = :sid",
            ConditionExpression="attribute_exists(connector_id) AND (#s = :available OR saga_id = :sid)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":reserved": "RESERVED", ":available": "AVAILABLE", ":sid": r.saga_id},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            log("reserve_rejected", r.saga_id, connector_id=r.connector_id)
            raise HTTPException(409, "connector not available")
        raise
    log("connector_reserved", r.saga_id, connector_id=r.connector_id)
    return {"status": "RESERVED"}


@app.post("/release")
def release(r: Req):
    """Compensación. Solo libera si el conector sigue tomado por ESTA saga -> procesarla dos veces no hace daño."""
    try:
        connectors.update_item(
            Key={"connector_id": r.connector_id},
            UpdateExpression="SET #s = :available REMOVE saga_id",
            ConditionExpression="saga_id = :sid",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":available": "AVAILABLE", ":sid": r.saga_id},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            log("release_already_done", r.saga_id, connector_id=r.connector_id)
            return {"status": "ALREADY_RELEASED"}
        raise
    log("connector_released", r.saga_id, connector_id=r.connector_id)
    return {"status": "AVAILABLE"}


@app.post("/confirm")
def confirm(r: Req):
    """Último paso de la saga exitosa: RESERVED -> CHARGING."""
    connectors.update_item(
        Key={"connector_id": r.connector_id},
        UpdateExpression="SET #s = :charging",
        ConditionExpression="saga_id = :sid",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":charging": "CHARGING", ":sid": r.saga_id},
    )
    log("connector_charging", r.saga_id, connector_id=r.connector_id)
    return {"status": "CHARGING"}
