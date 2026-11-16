"""Código compartido por los 4 servicios: nombres de tablas por ambiente, logs JSON y feature flags."""
import json
import logging
import os
import socket
import sys
import time

import boto3

ENV = os.getenv("ENV", "dev")
SERVICE = os.getenv("SERVICE", "unknown")
_endpoint = os.getenv("DYNAMODB_ENDPOINT") or None  # dynamodb-local en docker-compose/CI; vacío = AWS real
dynamodb = boto3.resource("dynamodb", endpoint_url=_endpoint)
client = boto3.client("dynamodb", endpoint_url=_endpoint)


def table_name(name: str) -> str:
    # Database-per-Service + aislamiento por ambiente: aegisvolt-billing-test, aegisvolt-dispatch-prod...
    return f"aegisvolt-{name}-{ENV}"


def table(name: str):
    return dynamodb.Table(table_name(name))


class _JsonFormatter(logging.Formatter):
    def format(self, r):
        return json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r.created)),
            "level": r.levelname,
            "env": ENV,
            "service": SERVICE,
            "correlation_id": getattr(r, "correlation_id", None),
            "event": r.getMessage(),
            **getattr(r, "fields", {}),
        }, default=str)


logger = logging.getLogger("aegisvolt")
logger.setLevel(logging.INFO)
_h = logging.StreamHandler(sys.stdout)
_h.setFormatter(_JsonFormatter())
logger.addHandler(_h)
if os.getenv("CLOUDWATCH") == "1":
    import watchtower  # envía el mismo JSON a /aegisvolt/<env>/<servicio>; el grupo lo crea Terraform

    _cw = watchtower.CloudWatchLogHandler(
        log_group_name=f"/aegisvolt/{ENV}/{SERVICE}",
        log_stream_name=socket.gethostname(),  # nombre del pod -> permite filtrar logs del canary
        create_log_group=False,
    )
    _cw.setFormatter(_JsonFormatter())
    logger.addHandler(_cw)


def log(event: str, correlation_id=None, level=logging.INFO, **fields):
    logger.log(level, event, extra={"correlation_id": correlation_id, "fields": fields})


_flags: dict = {}


def flag(name: str, ttl: float = 3.0) -> bool:
    """Lee la flag de aegisvolt-flags-<env> en cada request (caché de 3 s): cambiarla NO requiere redesplegar."""
    hit = _flags.get(name)
    if hit and time.time() - hit[1] < ttl:
        return hit[0]
    item = table("flags").get_item(Key={"flag": name}).get("Item") or {}
    value = bool(item.get("enabled", False))
    _flags[name] = (value, time.time())
    return value
