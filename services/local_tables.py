"""Crea y siembra las tablas en dynamodb-local (docker-compose y CI). En AWS las crea Terraform (infra/).
Mantener los datos semilla iguales a infra/modules/dynamodb/seed.tf."""
from common import ENV, client, table, table_name

KEYS = {"reservations": "connector_id", "billing": "pk", "dispatch": "session_id",
        "sagas": "saga_id", "flags": "flag"}

CONNECTORS = [
    ("CON-BOG-01-A", "Electrolinera Bogotá Norte", 150),
    ("CON-BOG-01-B", "Electrolinera Bogotá Norte", 150),
    ("CON-MED-01-A", "Electrolinera Medellín Poblado", 150),
    ("CON-CDMX-01-A", "Electrolinera CDMX Vallejo", 350),
    ("CON-LIM-01-A", "Electrolinera Lima Callao", 350),
]
WALLETS = {"fleet-rapiexpress": 500, "fleet-sinsaldo": 1}
FLAGS = {  # mismo valor por defecto que Terraform para dev/test
    "ENABLE_DYNAMIC_SURGE_PRICING": True,
    "ENABLE_FAST_CHARGE_BOOST": True,
    "SIMULATE_CHARGER_HARDWARE_FAULT": False,
}


def reset():
    existing = client.list_tables()["TableNames"]
    for name, key in KEYS.items():
        tn = table_name(name)
        if tn in existing:
            client.delete_table(TableName=tn)
            client.get_waiter("table_not_exists").wait(TableName=tn)
        client.create_table(TableName=tn, BillingMode="PAY_PER_REQUEST",
                            KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                            AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}])
        client.get_waiter("table_exists").wait(TableName=tn)
    for cid, station, kw in CONNECTORS:
        table("reservations").put_item(Item={"connector_id": cid, "station_name": station, "power_kw": kw,
                                             "status": "AVAILABLE"})
    for fleet, bal in WALLETS.items():
        table("billing").put_item(Item={"pk": f"WALLET#{fleet}", "fleet_id": fleet, "balance": bal})
    for f, v in FLAGS.items():
        table("flags").put_item(Item={"flag": f, "enabled": v})


if __name__ == "__main__":
    reset()
    print(f"tablas aegisvolt-*-{ENV} listas")
