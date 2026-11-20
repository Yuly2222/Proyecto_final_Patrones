#!/usr/bin/env bash
# Despliegue Canary a Kubernetes con Helm:  scripts/deploy.sh <dev|test|prod> <tag-de-imagen>
#   1. helm upgrade con canaryTag=<nuevo>: crea <svc>-canary (1 réplica) junto a <svc>-stable (4)  -> ~20 % del tráfico
#   2. genera tráfico de prueba y evalúa: pods canary Ready, 0 reinicios, 0 respuestas no-200 y
#      errores en CloudWatch (Floci) de los streams del canary <= ERROR_THRESHOLD
#   3. si pasa -> helm upgrade con stableTag=<nuevo> (100 %, el canary desaparece)
#      si falla -> helm rollback a la revisión anterior (solo la versión estable) y sale con error
set -euo pipefail

ENV=$1
TAG=$2
NS="aegisvolt-$ENV"
CHART=charts/aegisvolt
VALUES="$CHART/values-$ENV.yaml"
[ -f "$VALUES" ] || { echo "ambiente inválido: $ENV"; exit 1; }
ERROR_THRESHOLD=${ERROR_THRESHOLD:-0}
SMOKE_REQUESTS=${SMOKE_REQUESTS:-40}
URL="http://localhost:$(sed -n 's/^nodePort: *//p' "$VALUES")"
K="kubectl -n $NS"

helm_deploy() { # --set ... extra
  helm upgrade --install aegisvolt "$CHART" -n "$NS" --create-namespace -f "$VALUES" --wait --timeout 3m "$@"
}

smoke_traffic() { # cuenta respuestas no-200 de consultas que tocan los 4 servicios
  local fails=0
  for _ in $(seq "$SMOKE_REQUESTS"); do
    code=$(curl -s -o /dev/null -w '%{http_code}' -H 'content-type: application/json' "$URL/graphql" \
      -d '{"query":"{ stations { connectorId } wallets { fleetId } telemetry { sessionId } featureFlags { name } }"}' || true)
    [ "$code" = "200" ] || fails=$((fails + 1))
    sleep 0.5
  done
  echo "$fails"
}

cloudwatch_errors() { # start_ms -> errores ERROR en los log streams de pods canary (el stream = nombre del pod)
  # Se consulta desde un pod del gateway: ya tiene boto3 y AWS_ENDPOINT_URL apuntando a Floci.
  $K exec deploy/gateway-stable -- python -c "
import boto3
logs, n = boto3.client('logs'), 0
for name, svc in [('reservation', 'station-reservation-service'), ('billing', 'billing-wallet-service'),
                  ('dispatch', 'energy-dispatch-service'), ('gateway', 'graphql-gateway')]:
    n += len(logs.filter_log_events(logGroupName='/aegisvolt/$ENV/' + svc, logStreamNamePrefix=name + '-canary',
                                    startTime=$1, filterPattern='{ \$.level = \"ERROR\" }')['events'])
print(n)"
}

canary_healthy() { # start_ms
  local http_fails restarts cw_errors
  http_fails=$(smoke_traffic)
  restarts=$($K get pods -l track=canary -o jsonpath='{.items[*].status.containerStatuses[*].restartCount}' |
    tr ' ' '\n' | awk '{s += $1} END {print s + 0}')
  cw_errors=$(cloudwatch_errors "$1")
  echo "canary -> respuestas_fallidas=$http_fails reinicios=$restarts errores_cloudwatch=$cw_errors (umbral=$ERROR_THRESHOLD)"
  [ "$http_fails" -eq 0 ] && [ "$restarts" -eq 0 ] && [ "$cw_errors" -le "$ERROR_THRESHOLD" ]
}

current=$($K get deploy gateway-stable -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null | cut -d: -f2 || true)

if [ -z "$current" ] || [ "$current" = "$TAG" ]; then # primer despliegue (nada que proteger) o mismo código
  helm_deploy --set stableTag="$TAG"
else
  echo ">>> CANARY $TAG (1 réplica por servicio) junto a $current (4 réplicas)"
  start_ms=$(($(date +%s) * 1000))
  if helm_deploy --set stableTag="$current" --set canaryTag="$TAG" && canary_healthy "$start_ms"; then
    echo ">>> PROMOVER $TAG al 100 %"
    helm_deploy --set stableTag="$TAG"
  else
    echo ">>> ROLLBACK: helm vuelve a la revisión anterior; $current sigue atendiendo el 100 %"
    helm rollback aegisvolt -n "$NS" --wait
    helm history aegisvolt -n "$NS" --max 5
    exit 1
  fi
fi

helm history aegisvolt -n "$NS" --max 5
echo "Despliegue $ENV OK -> $URL"
