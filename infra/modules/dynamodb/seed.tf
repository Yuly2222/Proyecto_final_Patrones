# Datos iniciales. Mantener iguales a services/local_tables.py.
# ignore_changes = [item]: Terraform crea el ítem una vez y luego NO revierte los cambios hechos en
# caliente (balances, estados de conectores, flags cambiadas desde el frontend o la CLI).
locals {
  # Cada ambiente tiene su propio estado de flags (requisito 2.2).
  flags = {
    dev  = { ENABLE_DYNAMIC_SURGE_PRICING = true, ENABLE_FAST_CHARGE_BOOST = true, SIMULATE_CHARGER_HARDWARE_FAULT = false }
    test = { ENABLE_DYNAMIC_SURGE_PRICING = true, ENABLE_FAST_CHARGE_BOOST = false, SIMULATE_CHARGER_HARDWARE_FAULT = false }
    prod = { ENABLE_DYNAMIC_SURGE_PRICING = false, ENABLE_FAST_CHARGE_BOOST = false, SIMULATE_CHARGER_HARDWARE_FAULT = false }
  }
  connectors = {
    "CON-BOG-01-A"  = { station = "Electrolinera Bogotá Norte", kw = 150 }
    "CON-BOG-01-B"  = { station = "Electrolinera Bogotá Norte", kw = 150 }
    "CON-MED-01-A"  = { station = "Electrolinera Medellín Poblado", kw = 150 }
    "CON-CDMX-01-A" = { station = "Electrolinera CDMX Vallejo", kw = 350 }
    "CON-LIM-01-A"  = { station = "Electrolinera Lima Callao", kw = 350 }
  }
  wallets = { "fleet-rapiexpress" = 500, "fleet-sinsaldo" = 1 }
}

resource "aws_dynamodb_table_item" "flags" {
  for_each   = local.flags[var.env]
  table_name = aws_dynamodb_table.this["flags"].name
  hash_key   = "flag"
  item       = jsonencode({ flag = { S = each.key }, enabled = { BOOL = each.value } })
  lifecycle {
    ignore_changes = [item]
  }
}

resource "aws_dynamodb_table_item" "connectors" {
  for_each   = local.connectors
  table_name = aws_dynamodb_table.this["reservations"].name
  hash_key   = "connector_id"
  item = jsonencode({
    connector_id = { S = each.key }, station_name = { S = each.value.station },
    power_kw     = { N = tostring(each.value.kw) }, status = { S = "AVAILABLE" }
  })
  lifecycle {
    ignore_changes = [item]
  }
}

resource "aws_dynamodb_table_item" "wallets" {
  for_each   = local.wallets
  table_name = aws_dynamodb_table.this["billing"].name
  hash_key   = "pk"
  item = jsonencode({
    pk = { S = "WALLET#${each.key}" }, fleet_id = { S = each.key }, balance = { N = tostring(each.value) }
  })
  lifecycle {
    ignore_changes = [item]
  }
}
