# Aprendizajes del primer corte → dónde se ven en AegisVolt

Cada tema visto en clase, en qué parte del proyecto aparece y cómo mostrarlo en la sustentación.

| # | Tema | Dónde está en el proyecto | Estado |
|---|---|---|---|
| 1 | Saga | `services/gateway/saga.py` | ✅ |
| 2 | Orquestación | `services/gateway/saga.py` (`run_saga`) | ✅ |
| 3 | Compensación | `saga.py` (`COMPENSATIONS`, `compensate`), `/refund` y `/release` | ✅ |
| 4 | Idempotencia | Escrituras condicionales en `reservation`, `billing`, `dispatch` | ✅ |
| 5 | Conmutatividad | Llaves por `saga_id` + compensaciones tolerantes al orden | ✅ (parcial, ver nota) |
| 6 | Retriable | Reintentos de pasos y de compensaciones en `saga.py` | ✅ |
| 7 | Docker | `services/Dockerfile`, `frontend/Dockerfile`, `docker-compose.yml` | ✅ |
| 8 | Contenedores | Un contenedor por microservicio, orquestados por Kubernetes (`k8s/`) | ✅ |
| 9 | Buenas prácticas Docker | Imagen `slim`, `USER nobody`, `--no-cache-dir`, `.dockerignore`, versiones fijadas | ✅ |
| 10 | Arquitectura Docker | Cliente → daemon → imágenes/capas → contenedores; red de compose | ✅ |
| 11 | Multi-stage | `frontend/Dockerfile` (node build → nginx) | ✅ |
| 12 | Routing patterns | nginx como reverse proxy, GraphQL Gateway, Service de K8s | ✅ |
| 13 | Canary | `scripts/deploy.sh` + `k8s/app.yaml` (stable ×4 + canary ×1 = 20 %) | ✅ |
| 14 | Apache Kafka | No se usa (comunicación síncrona HTTP) | ❌ explicado abajo |

---

## 1. Saga

**Concepto:** en microservicios con *Database-per-Service* no hay transacción ACID global. Una saga divide la
operación en transacciones locales; si una falla se deshacen las anteriores con compensaciones.

**En el proyecto:** la saga *"Reservar y Activar Carga"* tiene 3 pasos, cada uno en un servicio con su propia tabla
DynamoDB:

```
RESERVE (reservation) → CHARGE (billing) → START_CHARGER (dispatch) → CONFIRM
```

El estado de cada saga se guarda en la tabla `aegisvolt-sagas-<env>` (*saga log*), que audita Prefect
(`observability/flows.py`).

## 2. Orquestación (vs. coreografía)

**Concepto:** en la **orquestación** un coordinador central dice a cada servicio qué hacer; en la **coreografía**
cada servicio reacciona a eventos de los demás, sin coordinador.

**En el proyecto:** se usa **orquestación**. El `graphql-gateway` es el orquestador (`run_saga` en
`services/gateway/saga.py`): llama a cada servicio en orden y decide cuándo compensar.

*Por qué orquestación:* el flujo es corto (3 pasos), el orden importa (no cobrar sin reservar) y queda un único
lugar donde leer y auditar el flujo. Coreografía tendría sentido con muchos consumidores independientes del
mismo evento (ver Kafka abajo).

## 3. Compensación

**Concepto:** una compensación es la acción de negocio que *semánticamente* deshace un paso ya confirmado
(no es un rollback de BD). Se ejecutan en **orden inverso**.

**En el proyecto:**

| Paso hecho | Compensación |
|---|---|
| `CHARGE` | `billing /refund` (reembolso) |
| `RESERVE` | `reservation /release` (liberar conector) |

Demo: activar el flag `SIMULATE_CHARGER_HARDWARE_FAULT` → el paso 3 falla → se ve en el historial
`COMPENSATE_CHARGE` y luego `COMPENSATE_RESERVE`, estado final `COMPENSATED`.
Test: `test_charger_fault_refunds_then_releases` en `services/tests/test_unit_saga.py`.

## 4. Idempotencia

**Concepto:** ejecutar la misma operación N veces deja el mismo resultado que ejecutarla 1 vez. Es lo que hace
seguro reintentar.

**En el proyecto:** todas las operaciones usan el `saga_id` como llave de idempotencia con escrituras
**condicionales** de DynamoDB:

- `reservation /reserve`: solo reserva si está `AVAILABLE` **o** si ya la reservó esa misma saga.
- `billing /charge` y `/refund`: escriben `TXN#<saga_id>#CHARGE|REFUND` con `attribute_not_exists(pk)` dentro de una
  transacción → no se cobra ni se reembolsa dos veces.
- `dispatch /start`: `attribute_not_exists(session_id)`.

## 5. Conmutatividad

**Concepto:** operaciones conmutativas dan el mismo resultado sin importar el orden en que lleguen
(`a ∘ b = b ∘ a`). Importa cuando hay mensajes desordenados o concurrencia.

**En el proyecto:**

- Las operaciones de distintas sagas no interfieren: cada una escribe sobre su propia llave `TXN#<saga_id>#...`,
  así que dos sagas sobre billeteras/conectores distintos pueden aplicarse en cualquier orden.
- `release` solo actúa si el conector pertenece a esa `saga_id` (`ConditionExpression="saga_id = :sid"`), por lo
  que una liberación tardía de una saga vieja no pisa la reserva de una nueva.
- *Límite honesto:* `CHARGE` y `REFUND` de la **misma** saga no son conmutativos (no se puede reembolsar antes de
  cobrar). Eso se resuelve con la orquestación, que garantiza el orden.

## 6. Retriable

**Concepto:** distinguir errores **transitorios** (red caída, 5xx, timeout → reintentar) de errores de **negocio**
(409 conector ocupado, 402 saldo insuficiente → reintentar no sirve, hay que compensar). Reintentar solo es seguro
si la operación es idempotente (punto 4).

**En el proyecto:**

- `StepFailed(retriable=...)` en `saga.py`; `http_call` en `gateway/main.py` marca como reintentable los errores de
  red y los 5xx.
- Los **pasos** se reintentan hasta `MAX_RETRIES = 3` con backoff corto; si siguen fallando, se compensa.
- Las **compensaciones** se reintentan siempre (deben terminar); si agotan los intentos la saga queda
  `COMPENSATION_FAILED` y Prefect la reporta.
- Tests: `test_transient_step_failure_is_retried_not_compensated`, `test_compensation_is_retried`,
  `test_compensation_gives_up_after_max_retries`.

## 7. Docker

**Concepto:** empaquetar la app con su runtime y dependencias en una **imagen** inmutable que corre igual en
cualquier máquina.

**En el proyecto:** `services/Dockerfile` es una sola receta que genera una imagen por microservicio con
`--build-arg SERVICE=billing|reservation|dispatch|gateway`. `docker compose up -d --build` levanta todo el stack con
`dynamodb-local` (lo usa también el job e2e del CI).

## 8. Contenedores

**Concepto:** instancia en ejecución de una imagen; proceso aislado (namespaces, cgroups) que comparte el kernel del
host. Más livianos que una VM.

**En el proyecto:** cada microservicio corre en su contenedor; en Kubernetes cada uno tiene `requests/limits` de
CPU y memoria y probes de `readiness`/`liveness` sobre `/health` (`k8s/app.yaml`).

## 9. Buenas prácticas Docker

| Práctica | Dónde |
|---|---|
| Imagen base pequeña (`python:3.12-slim`, `alpine`) | ambos Dockerfile |
| Versiones fijadas (no `latest`) | `nginx:1.27-alpine`, `dynamodb-local:2.5.4`, `requirements.txt` con `==` |
| Copiar `requirements.txt` antes del código (aprovecha la **caché de capas**) | `services/Dockerfile` |
| `pip install --no-cache-dir` | `services/Dockerfile` |
| No correr como root (`USER nobody`) | `services/Dockerfile` |
| `.dockerignore` (no mandar `node_modules`, tests, cachés al contexto) | `frontend/.dockerignore`, `services/.dockerignore` |
| Un proceso por contenedor | todos |
| Config por variables de entorno, no horneada en la imagen | `ENV`, ConfigMap y Secret en K8s |
| Logs a stdout (`PYTHONUNBUFFERED=1`) | `services/Dockerfile` |

## 10. Arquitectura Docker

**Concepto:** el **cliente** (`docker` CLI) habla con el **daemon** (`dockerd`) por una API; el daemon construye
**imágenes** (capas de solo lectura apiladas), crea **contenedores** (imagen + capa escribible) y los conecta a
**redes** y **volúmenes**. Las imágenes se guardan en un **registry**.

**En el proyecto:** `docker compose` crea una red interna donde los servicios se resuelven por nombre
(`http://billing:8000`), igual que los Services de Kubernetes. Kubernetes (Docker Desktop) usa las imágenes
locales (`imagePullPolicy: IfNotPresent`).

## 11. Multi-stage

**Concepto:** varias etapas `FROM` en un Dockerfile; se compila en una etapa con herramientas pesadas y solo se
copia el resultado a una imagen final pequeña.

**En el proyecto:** `frontend/Dockerfile`:

```dockerfile
FROM node:20-alpine AS build      # etapa 1: node + npm + código → npm run build
...
FROM nginx:1.27-alpine            # etapa 2: solo nginx + /dist estático
COPY --from=build /app/dist /usr/share/nginx/html
```

La imagen final no lleva Node ni `node_modules`. En `services/` no se usa multi-stage porque las dependencias de
Python son wheels precompiladas: no hay paso de compilación que descartar.

## 12. Routing patterns

**Concepto:** patrones para decidir a dónde va una petición: *API Gateway*, *reverse proxy*, *content-based routing*,
*weighted routing*, etc.

**En el proyecto:**

- **Reverse proxy / same-origin:** `frontend/nginx.conf` envía `/graphql` al gateway de su mismo namespace.
- **API Gateway / Aggregation:** el `graphql-gateway` expone un solo esquema y enruta cada campo al microservicio
  correcto.
- **Weighted routing:** el Service de K8s selecciona solo por `app` → reparte entre `stable` y `canary` según el
  número de réplicas (base del canary).
- **Ambientes:** cada ambiente tiene su propio NodePort (30080/30081/30082) y namespace.
- **API Gateway privado** de AWS restringido a la VPC (`infra/modules/apigateway/`).

## 13. Canary

**Concepto:** liberar la versión nueva a un porcentaje pequeño del tráfico, medir, y promover o hacer rollback.

**En el proyecto:** `scripts/deploy.sh`

1. Crea `<svc>-canary` (1 réplica) junto a `<svc>-stable` (4 réplicas) → ~20 % del tráfico.
2. Genera tráfico y evalúa: readiness OK, 0 reinicios, 0 respuestas no-200, errores en CloudWatch ≤ umbral.
3. Pasa → actualiza stable y borra el canary. Falla → borra el canary (**rollback**) y el pipeline falla.

## 14. Apache Kafka

**Concepto:** log de eventos distribuido, particionado y replicado. Productores publican en **topics**, consumidores
en **consumer groups** leen a su ritmo con **offsets**. Entrega *at-least-once* por defecto → los consumidores deben
ser **idempotentes**. Habilita coreografía y desacople temporal.

**En el proyecto: no se usa.** La saga es síncrona por HTTP y orquestada, y el enunciado no lo pide. Dónde
encajaría si se agregara:

- Publicar los eventos de la saga (`saga_started`, `saga_completed`, `saga_rolled_back`) en un topic
  `aegisvolt.sagas` para que otros consumidores (notificaciones, analítica, Prefect) reaccionen sin acoplarse al
  gateway.
- La telemetría de los cargadores (`dispatch`) como stream, en vez de consultas por polling.
- Pasar de orquestación a **coreografía**: cada servicio consume el evento del paso anterior y publica el suyo.

*Cómo defenderlo:* las operaciones ya son idempotentes por `saga_id`, que es exactamente lo que exige Kafka con
*at-least-once*, así que el diseño está preparado para migrar a mensajería sin cambiar la lógica de negocio.
