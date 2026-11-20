"""Código compartido por los 4 servicios: nombres de tablas por ambiente, logs JSON y feature flags."""
import json
import logging
import os
import socket
import sys
import time

import boto3
from opentelemetry import trace

ENV = os.getenv("ENV", "dev")
SERVICE = os.getenv("SERVICE", "unknown")
# boto3 toma el endpoint de AWS_ENDPOINT_URL: Floci en Kubernetes, docker-compose y CI (ver README).
dynamodb = boto3.resource("dynamodb")
client = boto3.client("dynamodb")


def table_name(name: str) -> str:
    # Database-per-Service + aislamiento por ambiente: aegisvolt-billing-test, aegisvolt-dispatch-prod...
    return f"aegisvolt-{name}-{ENV}"


def table(name: str):
    return dynamodb.Table(table_name(name))


class _JsonFormatter(logging.Formatter):
    def format(self, r):
        span = trace.get_current_span().get_span_context()
        return json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r.created)),
            "level": r.levelname,
            "env": ENV,
            "service": SERVICE,
            "correlation_id": getattr(r, "correlation_id", None),
            "trace_id": format(span.trace_id, "032x") if span.is_valid else None,  # mismo id que en Jaeger
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
        send_interval=2,  # por defecto agrupa 60 s: el canary consultaría CloudWatch antes de que lleguen los errores
    )
    _cw.setFormatter(_JsonFormatter())
    logger.addHandler(_cw)


def log(event: str, correlation_id=None, level=logging.INFO, **fields):
    logger.log(level, event, extra={"correlation_id": correlation_id, "fields": fields})


def setup_tracing(app):
    """OpenTelemetry -> Jaeger. Cada request HTTP entrante abre una traza; las llamadas httpx (gateway ->
    servicios) propagan el contexto en el header `traceparent` y las de boto3 (DynamoDB) cuelgan como hijas.
    Sin OTEL_EXPORTER_OTLP_ENDPOINT no hace nada (tests unitarios)."""
    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON, ParentBased, Sampler

    class OnlyRequests(Sampler):  # raíz = request entrante; descarta, p. ej., los PutLogEvents de watchtower
        def should_sample(self, parent_context, trace_id, name, kind=None, *args, **kwargs):
            sampler = ALWAYS_ON if kind == trace.SpanKind.SERVER else ALWAYS_OFF
            return sampler.should_sample(parent_context, trace_id, name, kind, *args, **kwargs)

        def get_description(self):
            return "OnlyRequests"

    provider = TracerProvider(sampler=ParentBased(root=OnlyRequests()), resource=Resource.create(
        {"service.name": SERVICE, "deployment.environment": ENV}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))  # lee el endpoint de la variable
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()
    BotocoreInstrumentor().instrument()
    FastAPIInstrumentor.instrument_app(app, excluded_urls="health")  # sin trazas de las probes de K8s


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
