#!/usr/bin/env bash
# Despliegue Canary a Kubernetes:  scripts/deploy.sh <dev|test|prod> <tag-de-imagen>
# Por cada microservicio cuya imagen cambió:
#   1. crea <svc>-canary con 1 réplica junto a <svc>-stable (4 réplicas)  -> ~20 % del tráfico
#   2. genera tráfico de prueba y evalúa: readiness OK, 0 reinicios, 0 respuestas 5xx y
#      errores en CloudWatch del stream del canary <= ERROR_THRESHOLD
#   3. si pasa -> actualiza stable al 100 % y borra el canary; si falla -> borra el canary (rollback) y sale con error
set -euo pipefail

ENV=$1
TAG=$2
NS="aegisvolt-$ENV"
STABLE_REPLICAS=${STABLE_REPLICAS:-4}
ERROR_THRESHOLD=${ERROR_THRESHOLD:-0}
SMOKE_REQUESTS=${SMOKE_REQUESTS:-40}
case $ENV in dev) NODEPORT=30080 ;; test) NODEPORT=30081 ;; prod) NODEPORT=30082 ;; *) echo "ambiente inválido"; exit 1 ;; esac
URL="http://localhost:$NODEPORT"
SERVICES="reservation:station-reservation-service billing:billing-wallet-service dispatch:energy-dispatch-service gateway:graphql-gateway"
K="kubectl -n $NS"

render() { # name service image track replicas
  sed -e "s|__NAME__|$1|g" -e "s|__SERVICE__|$2|g" -e "s|__IMAGE__|$3|g" -e "s|__TRACK__|$4|g" \
      -e "s|__REPLICAS__|$5|g" k8s/app.yaml
}

# --- Namespace, configuración y credenciales del ambiente ---
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -
$K create configmap aegisvolt-config --from-literal=ENV="$ENV" --from-literal=CLOUDWATCH=1 \
  --from-literal=AWS_DEFAULT_REGION=us-east-1 --dry-run=client -o yaml | $K apply -f -
$K create secret generic aws-creds --from-literal=AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
  --from-literal=AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" --dry-run=client -o yaml | $K apply -f -

# --- Services primero (nginx necesita resolver "gateway" al arrancar), luego el frontend ---
for entry in $SERVICES; do sed "s|__NAME__|${entry%%:*}|g" k8s/service.yaml | $K apply -f -; done
sed -e "s|__IMAGE__|aegisvolt/frontend:$TAG|" -e "s|__NODEPORT__|$NODEPORT|" k8s/frontend.yaml | $K apply -f -

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

canary_healthy() { # name service start_ms
  local restarts http_fails cw_errors
  http_fails=$(smoke_traffic)
  restarts=$($K get pods -l "app=$1,track=canary" -o jsonpath='{.items[*].status.containerStatuses[*].restartCount}')
  cw_errors=$(aws logs filter-log-events --log-group-name "/aegisvolt/$ENV/$2" --log-stream-name-prefix "$1-canary" \
    --start-time "$3" --filter-pattern '{ $.level = "ERROR" }' --query 'length(events)' --output text 2>/dev/null || echo 0)
  echo "canary $1 -> respuestas_fallidas=$http_fails reinicios=${restarts:-0} errores_cloudwatch=$cw_errors (umbral=$ERROR_THRESHOLD)"
  [ "$http_fails" -eq 0 ] && [ "${restarts:-0}" -eq 0 ] && [ "$cw_errors" -le "$ERROR_THRESHOLD" ]
}

for entry in $SERVICES; do
  name=${entry%%:*}; svc=${entry#*:}; image="aegisvolt/$name:$TAG"

  if ! $K get deploy "$name-stable" >/dev/null 2>&1; then # primer despliegue: no hay versión estable que proteger
    render "$name" "$svc" "$image" stable "$STABLE_REPLICAS" | $K apply -f -
    continue
  fi
  current=$($K get deploy "$name-stable" -o jsonpath='{.spec.template.spec.containers[0].image}')
  [ "$current" = "$image" ] && { echo "$name sin cambios"; continue; }

  echo ">>> CANARY $name: $image (1 réplica) junto a $current ($STABLE_REPLICAS réplicas)"
  start_ms=$(($(date +%s) * 1000))
  render "$name" "$svc" "$image" canary 1 | $K apply -f -
  if $K rollout status "deploy/$name-canary" --timeout=120s && canary_healthy "$name" "$svc" "$start_ms"; then
    echo ">>> PROMOVER $name al 100 %"
    render "$name" "$svc" "$image" stable "$STABLE_REPLICAS" | $K apply -f -
    $K rollout status "deploy/$name-stable" --timeout=180s
    $K delete deploy "$name-canary"
  else
    echo ">>> ROLLBACK $name: se elimina el canary, la versión estable $current sigue atendiendo el 100 %"
    $K delete deploy "$name-canary" --ignore-not-found
    exit 1
  fi
done

for entry in $SERVICES; do $K rollout status "deploy/${entry%%:*}-stable" --timeout=180s; done
echo "Despliegue $ENV OK -> $URL"
