"""billing-wallet-service: verificación de fondos, cobro y reembolso (tabla aegisvolt-billing-<env>).

Ítems en la misma tabla:
  pk = WALLET#<fleet_id>          -> balance de la flota
  pk = TXN#<saga_id>#CHARGE|REFUND -> movimiento; su existencia es la llave de idempotencia
"""
import time
from decimal import Decimal

from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from common import ENV, client, flag, log, table, table_name

app = FastAPI(title="billing-wallet-service")
TABLE = table_name("billing")
billing = table("billing")
RATE_PER_KWH = Decimal("0.35")  # USD
SURGE_MULTIPLIER = Decimal("1.5")


class ChargeReq(BaseModel):
    saga_id: str
    fleet_id: str
    kwh: float = Field(gt=0, le=500)  # kwh negativo = cobro negativo = la billetera ganaría saldo


class RefundReq(BaseModel):
    saga_id: str


@app.get("/health")
def health():
    return {"status": "ok", "env": ENV}


@app.get("/wallets")
def wallets():
    return [i for i in billing.scan()["Items"] if i["pk"].startswith("WALLET#")]


@app.get("/transactions")
def transactions():
    return [i for i in billing.scan()["Items"] if i["pk"].startswith("TXN#")]


def price(kwh: float) -> tuple[Decimal, bool]:
    surge = flag("ENABLE_DYNAMIC_SURGE_PRICING")
    amount = Decimal(str(kwh)) * RATE_PER_KWH * (SURGE_MULTIPLIER if surge else 1)
    return amount.quantize(Decimal("0.01")), surge


def _reasons(e: ClientError) -> list[str]:
    return [r.get("Code", "None") for r in e.response.get("CancellationReasons", [])]


@app.post("/charge")
def charge(r: ChargeReq):
    amount, surge = price(r.kwh)
    a = {"N": str(amount)}
    try:
        # Una sola transacción DynamoDB: registra el TXN (solo si no existe) y descuenta (solo si alcanza el saldo).
        client.transact_write_items(TransactItems=[
            {"Put": {
                "TableName": TABLE,
                "Item": {"pk": {"S": f"TXN#{r.saga_id}#CHARGE"}, "saga_id": {"S": r.saga_id}, "type": {"S": "CHARGE"},
                         "fleet_id": {"S": r.fleet_id}, "amount": a, "surge": {"BOOL": surge},
                         "at": {"N": str(int(time.time()))}},
                "ConditionExpression": "attribute_not_exists(pk)",
            }},
            {"Update": {
                "TableName": TABLE,
                "Key": {"pk": {"S": f"WALLET#{r.fleet_id}"}},
                "UpdateExpression": "SET balance = balance - :a",
                "ConditionExpression": "attribute_exists(pk) AND balance >= :a",
                "ExpressionAttributeValues": {":a": a},
            }},
        ])
    except ClientError as e:
        if e.response["Error"]["Code"] != "TransactionCanceledException":
            raise
        reasons = _reasons(e)
        if reasons and reasons[0] == "ConditionalCheckFailed":
            log("charge_already_done", r.saga_id)  # reintento: no se cobra dos veces
            return {"status": "CHARGED", "amount": amount, "surge": surge}
        log("insufficient_funds", r.saga_id, fleet_id=r.fleet_id, amount=amount)
        raise HTTPException(402, "insufficient funds")
    log("wallet_charged", r.saga_id, fleet_id=r.fleet_id, amount=amount, surge=surge)
    return {"status": "CHARGED", "amount": amount, "surge": surge}


@app.post("/refund")
def refund(r: RefundReq):
    """Compensación del paso 2. Idempotente: el TXN#...#REFUND solo puede escribirse una vez."""
    charged = billing.get_item(Key={"pk": f"TXN#{r.saga_id}#CHARGE"}).get("Item")
    if not charged:
        return {"status": "NOTHING_TO_REFUND"}
    a = {"N": str(charged["amount"])}
    try:
        client.transact_write_items(TransactItems=[
            {"Put": {
                "TableName": TABLE,
                "Item": {"pk": {"S": f"TXN#{r.saga_id}#REFUND"}, "saga_id": {"S": r.saga_id}, "type": {"S": "REFUND"},
                         "fleet_id": {"S": charged["fleet_id"]}, "amount": a, "at": {"N": str(int(time.time()))}},
                "ConditionExpression": "attribute_not_exists(pk)",
            }},
            {"Update": {
                "TableName": TABLE,
                "Key": {"pk": {"S": f"WALLET#{charged['fleet_id']}"}},
                "UpdateExpression": "SET balance = balance + :a",
                "ExpressionAttributeValues": {":a": a},
            }},
        ])
    except ClientError as e:
        if e.response["Error"]["Code"] == "TransactionCanceledException" and _reasons(e)[:1] == ["ConditionalCheckFailed"]:
            log("refund_already_done", r.saga_id)
            return {"status": "ALREADY_REFUNDED"}
        raise
    log("wallet_refunded", r.saga_id, amount=charged["amount"])
    return {"status": "REFUNDED", "amount": charged["amount"]}
