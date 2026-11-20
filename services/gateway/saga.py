"""Orquestador SAGA "Reservar y Activar Carga" (SAGA por orquestación).

Cada paso es una llamada HTTP a un microservicio. Si un paso falla se ejecutan, en orden inverso,
las compensaciones de los pasos ya hechos. El estado de cada saga se guarda en aegisvolt-sagas-<env>
(es el "saga log" que audita Prefect).
"""
import logging
import time
import uuid
from typing import Callable

from common import log, table

MAX_RETRIES = 3


class StepFailed(Exception):
    """retriable=True: fallo transitorio (red, 5xx) -> se reintenta el mismo paso.
    retriable=False: error de negocio (409, 402...) -> reintentar no cambia nada, se compensa."""

    def __init__(self, msg: str = "", retriable: bool = False):
        super().__init__(msg)
        self.retriable = retriable


def _now():
    return int(time.time())


class SagaLog:
    def __init__(self, tbl=None):
        self.tbl = tbl or table("sagas")

    def start(self, saga: dict):
        self.tbl.put_item(Item=saga)

    def step(self, saga: dict, name: str, ok: bool, detail: str = ""):
        saga["steps"].append({"step": name, "ok": ok, "detail": detail, "at": _now()})
        self.save(saga)

    def save(self, saga: dict):
        saga["updated_at"] = _now()
        self.tbl.put_item(Item=saga)


def run_saga(call: Callable[[str, str, dict], dict], connector_id: str, fleet_id: str, kwh: float,
             saga_log: SagaLog | None = None) -> dict:
    """call(service, path, payload) -> dict JSON; lanza StepFailed si el servicio responde error."""
    saga_log = saga_log or SagaLog()
    sid = str(uuid.uuid4())  # = correlation_id que viaja en todos los logs
    saga = {"saga_id": sid, "connector_id": connector_id, "fleet_id": fleet_id, "kwh": str(kwh),
            "status": "STARTED", "steps": [], "created_at": _now()}
    saga_log.start(saga)
    log("saga_started", sid, connector_id=connector_id, fleet_id=fleet_id)
    ids = {"saga_id": sid, "connector_id": connector_id}
    done: list[str] = []

    steps = [
        ("RESERVE", "reservation", "/reserve", ids, "RESERVED"),
        ("CHARGE", "billing", "/charge", {"saga_id": sid, "fleet_id": fleet_id, "kwh": kwh}, "CHARGED"),
        ("START_CHARGER", "dispatch", "/start", ids, "DISPATCHED"),
    ]
    for name, service, path, payload, status in steps:
        try:
            for attempt in range(1, MAX_RETRIES + 1):  # Retriable: seguro porque cada paso es idempotente por saga_id
                try:
                    res = call(service, path, payload)
                    break
                except StepFailed as e:
                    if not e.retriable or attempt == MAX_RETRIES:
                        raise
                    log("step_retry", sid, step=name, attempt=attempt, error=str(e))
                    time.sleep(0.2 * attempt)  # backoff lineal corto
            if name == "CHARGE":
                saga["amount"] = str(res.get("amount"))
            done.append(name)
            saga["status"] = status
            saga_log.step(saga, name, True)
        except StepFailed as e:
            saga_log.step(saga, name, False, str(e))
            log("saga_step_failed", sid, step=name, error=str(e))
            if done:
                compensate(call, saga, done, saga_log)
            else:
                saga["status"] = "FAILED"  # falló el primer paso: no hay nada que deshacer
                saga_log.save(saga)
            return saga

    call("reservation", "/confirm", ids)
    saga["status"] = "COMPLETED"
    saga_log.step(saga, "CONFIRM", True)
    log("saga_completed", sid)
    return saga


COMPENSATIONS = {  # paso hecho -> (servicio, ruta) que lo deshace
    "CHARGE": ("billing", "/refund"),
    "RESERVE": ("reservation", "/release"),
}


def compensate(call, saga: dict, done: list[str], saga_log: SagaLog):
    saga["status"] = "COMPENSATING"
    saga_log.save(saga)
    payload = {"saga_id": saga["saga_id"], "connector_id": saga["connector_id"]}
    for name in reversed(done):  # orden inverso: primero reembolso, luego liberar conector
        if name not in COMPENSATIONS:
            continue
        service, path = COMPENSATIONS[name]
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                res = call(service, path, payload)  # seguro reintentar: las compensaciones son idempotentes
                saga_log.step(saga, f"COMPENSATE_{name}", True, res.get("status", ""))
                log("saga_compensated_step", saga["saga_id"], step=name, result=res.get("status"))
                break
            except StepFailed as e:
                log("compensation_retry", saga["saga_id"], step=name, attempt=attempt, error=str(e))
        else:
            saga["status"] = "COMPENSATION_FAILED"  # Prefect lo detecta y lo reporta
            saga_log.step(saga, f"COMPENSATE_{name}", False, "max retries")
            log("saga_compensation_failed", saga["saga_id"], logging.ERROR, step=name)
            return
    saga["status"] = "COMPENSATED"
    saga_log.save(saga)
    log("saga_rolled_back", saga["saga_id"])
