# Aprendizajes del curso → dónde se ven en AegisVolt

Semana 1 (temas 1-14) y semana 2 (temas 15-26). Cada tema visto en clase, en qué parte del proyecto aparece y cómo mostrarlo en la sustentación.

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

---

# Semana 2 — Resiliencia, GraphQL y ciberseguridad

| # | Tema | Dónde está en el proyecto | Estado |
|---|---|---|---|
| 15 | Circuit Breaker | `services/gateway/resilience.py` (`CircuitBreaker`), usado en `http_call` | ✅ |
| 16 | Bulkhead (Semaphore Bulkhead) | `resilience.py` (`Bulkhead`), uno por servicio | ✅ |
| 17 | Estrategias de Fallback | `gateway/main.py` (`read_with_fallback`) + compensaciones SAGA | ✅ |
| 18 | GraphQL | `gateway/main.py`: esquema único (Aggregation Layer) | ✅ |
| 19 | Seguridad en GraphQL | Límites de profundidad/tokens/alias, sin introspección en prod, solo POST | ✅ |
| 20 | MitM | VPC Endpoints privados + HTTPS hacia AWS | 🟡 parcial (sin TLS local) |
| 21 | Session Fixation | No hay sesiones: API sin estado | ➖ no aplica |
| 22 | CSRF | Solo POST + JSON, sin cookies, headers de seguridad en nginx | ✅ |
| 23 | Logins, Argon2, MFA | No hay login de usuarios | ❌ explicado abajo |
| 24 | Supply chain | Versiones fijadas, `package-lock.json`, `npm ci --ignore-scripts`, Dependabot | ✅ |
| 25 | Seguridad multimodal en IA | El sistema no usa IA | ➖ no aplica |
| 26 | Seguridad de agentes de IA | Límites de reintentos + circuit breaker (mismo principio) | 🟡 análogo |

## Patrones de resiliencia y aislamiento

### 15. Circuit Breaker

**Concepto:** si un servicio falla una y otra vez, dejar de llamarlo por un tiempo y fallar de inmediato, en vez de
esperar timeouts que bloquean hilos y empeoran la caída (fallo en cascada). Tiene 3 estados:

```
CLOSED ──(5 fallos seguidos)──► OPEN ──(15 s)──► HALF_OPEN ──1 éxito──► CLOSED
                                  ▲                  │
                                  └────1 fallo───────┘
```

**En el proyecto:** `BREAKERS` en `services/gateway/main.py` tiene **un breaker por microservicio**. Solo cuentan como
fallo los errores de red y los 5xx; un 4xx (409 conector ocupado, 402 sin saldo) es un error de negocio y el servicio
está sano. Con el circuito abierto, el paso de la saga falla al instante con `retriable=False` y se compensa sin
esperar.

*Demo:* activar `SIMULATE_CHARGER_HARDWARE_FAULT` (dispatch responde 503) y disparar dos reservas (cada una reintenta
3 veces). Al quinto fallo se abre el circuito de `dispatch` y el log muestra `dispatch circuit OPEN`. A los 15 s deja
pasar una llamada de prueba. Test: `test_circuit_breaker_opens_then_half_opens`.

### 16. Bulkhead / Semaphore Bulkhead

**Concepto:** como los compartimentos de un barco: se aíslan los recursos para que una avería no hunda todo. En la
variante **semaphore**, un semáforo limita cuántas llamadas simultáneas puede haber hacia cada dependencia.

**En el proyecto:** `BULKHEADS` en `gateway/main.py` tiene un `threading.BoundedSemaphore(10)` por servicio. Si
`billing` se pone lento, solo se agotan **sus** 10 cupos; las llamadas a `reservation` y `dispatch` siguen pasando
porque el threadpool del gateway no se queda bloqueado esperando a billing. Si no hay cupo en 1 s, la llamada se
rechaza con `bulkhead full` (reintentable). Test: `test_bulkhead_rejects_when_full`.

*Otros niveles de aislamiento que ya existían:* Database-per-Service (cada servicio con su propia tabla), namespaces
separados por ambiente y `limits` de memoria por pod en Kubernetes.

### 17. Estrategias de Fallback

**Concepto:** qué responder cuando la dependencia falla: valor por defecto, caché, respuesta degradada o acción
alternativa.

**En el proyecto** se usan dos estrategias distintas:

| Tipo de operación | Fallback | Dónde |
|---|---|---|
| **Lecturas** (`stations`, `wallets`, `telemetry`) | **Caché:** se devuelve la última respuesta buena (aunque esté desactualizada) o `[]` | `read_with_fallback` |
| **Escrituras** (la saga) | **Acción compensatoria:** reembolsar y liberar | `saga.py` |

Así, si `dispatch` se cae, el panel sigue mostrando estaciones y billeteras, y el log registra `fallback_used`.
En las escrituras no se usa caché porque no se puede "fingir" que se cobró. Test:
`test_read_fallback_serves_last_good_data`.

**Cómo se combinan los patrones en una llamada:**

```
saga / query ─► Retry (solo transitorios) ─► Circuit Breaker ─► Bulkhead ─► HTTP (timeout 5 s) ─► servicio
                       │ se agotan                │ abierto         │ lleno
                       ▼                          ▼                 ▼
             Fallback: compensación (escrituras) / caché (lecturas)
```

## GraphQL

### 18. GraphQL

**Concepto:** un único endpoint donde el cliente pide exactamente los campos que necesita; el esquema tipado es el
contrato. Las operaciones son *queries* (lectura) y *mutations* (escritura).

**En el proyecto:** el `graphql-gateway` (Strawberry) es un **GraphQL Aggregation Layer**: una sola consulta del
frontend junta datos de 3 microservicios y DynamoDB:

```graphql
{ stations { connectorId status } wallets { fleetId balance } telemetry { sessionId } featureFlags { name enabled } }
```

Mutations: `reserveAndCharge` (dispara la saga), `releaseConnector`, `setFeatureFlag`. El CI exporta el esquema como
artefacto en cada PR (`pr-checks.yml`).

### 19. Seguridad en GraphQL

GraphQL tiene riesgos propios porque es el cliente quien arma la consulta. Medidas en `gateway/main.py`:

| Riesgo | Medida |
|---|---|
| Consultas anidadas enormes (DoS) | `QueryDepthLimiter(max_depth=5)` |
| Documentos gigantes | `MaxTokensLimiter(1000)` + `client_max_body_size 16k` en nginx |
| **Alias batching** (la misma mutación ×1000 en un request, p. ej. fuerza bruta) | `MaxAliasesLimiter(15)`; test `test_graphql_alias_batching_is_blocked` |
| Reconocimiento del esquema | Introspección **deshabilitada en prod** (`NoSchemaIntrospectionCustomRule`) y sin GraphiQL |
| Fuga de detalles internos | `MaskErrors()` en prod: el cliente ve un error genérico y el detalle queda en CloudWatch |
| CSRF por GET | `allow_queries_via_get=False` + nginx `limit_except POST` |
| Validación de entrada | `kwh` debe estar entre 0 y 500 (`billing`). **Antes no se validaba:** con un `kwh` negativo el cobro salía negativo y la billetera *ganaba* saldo. Test: `test_negative_kwh_is_rejected` |

## Ciberseguridad para backend y frontend

### 20. Man-in-the-Middle (MitM)

**Concepto:** un atacante en medio de la comunicación lee o modifica el tráfico. Se previene con TLS (HTTPS),
HSTS y redes privadas.

**En el proyecto:**
- El tráfico hacia AWS (DynamoDB, CloudWatch) va por **HTTPS** (boto3) y, en AWS, por **VPC Endpoints**: no sale a
  internet (`infra/modules/network/`).
- El API Gateway es **PRIVATE**: solo se alcanza desde la VPC.
- *Límite honesto:* en local (Docker Desktop) el frontend se sirve por HTTP en `localhost`. En producción real se
  pondría TLS en un Ingress/ALB y el header `Strict-Transport-Security`, que no se agregó porque sin HTTPS no tiene
  efecto.

### 21. Session Fixation

**Concepto:** el atacante fija el ID de sesión de la víctima *antes* del login y lo reutiliza después. Se previene
regenerando el ID de sesión al autenticarse y usando cookies `HttpOnly; Secure; SameSite`.

**En el proyecto: no aplica.** La API es *stateless*: no hay cookies ni sesiones de servidor, así que no hay ID de
sesión que fijar. Si se agrega login (punto 23), hay que regenerar el ID de sesión o el token después de autenticar.

### 22. CSRF

**Concepto:** un sitio malicioso hace que el navegador de la víctima envíe una petición autenticada (con sus
cookies) a otro sitio.

**En el proyecto:**
- No hay cookies ni credenciales que el navegador envíe automáticamente → no hay nada que "secuestrar".
- GraphQL solo acepta **POST con `Content-Type: application/json`**. Un `<form>` de otro sitio no puede mandar
  ese content-type, y un `fetch` entre orígenes dispara un *preflight* CORS que el gateway no autoriza.
- GET a `/graphql` → **403** en nginx.
- CSP `form-action 'self'`.

**Headers de seguridad** agregados en `frontend/nginx.conf`: `Content-Security-Policy` (anti-XSS: solo scripts
propios), `X-Frame-Options: DENY` + `frame-ancestors 'none'` (anti-clickjacking), `X-Content-Type-Options: nosniff`,
`Referrer-Policy`, `Permissions-Policy` y `server_tokens off`.

### 23. Gestión de identidad: logins, Argon2, MFA

**Conceptos:**
- **Hashing de contraseñas:** nunca guardarlas en texto plano ni con hashes rápidos (MD5, SHA-256). **Argon2id**
  (ganador de la Password Hashing Competition) es lento a propósito y usa mucha memoria, lo que encarece la fuerza
  bruta con GPU. Incluye un *salt* por usuario.
- **MFA:** algo que sabes (contraseña) + algo que tienes (TOTP, llave FIDO2/passkey).
- **Logins seguros:** límite de intentos, mensajes genéricos ("usuario o contraseña inválidos") y regenerar la sesión.

**En el proyecto: no hay login de usuarios.** El enunciado no lo pide y el sistema se usa como panel de operador.
Lo que sí se protege son las **credenciales de máquina**:
- Las llaves de AWS van en un **Secret de Kubernetes** (`aws-creds`) y en **GitHub Secrets**, nunca en la imagen ni
  en el repositorio.
- Los contenedores corren como `USER nobody`.
- El API Gateway privado solo acepta tráfico desde la VPC.

*Cómo se agregaría:* un servicio `auth` con `argon2-cffi` (`PasswordHasher().hash()` / `.verify()`), TOTP con
`pyotp`, JWT de vida corta validado en el gateway, y el límite de alias de GraphQL (punto 19) contra la fuerza bruta.

### 24. Crisis de supply chain (2025-26)

**Concepto:** el atacante no ataca tu código sino una **dependencia**. En 2025 hubo paquetes populares de npm
comprometidos (cuentas de mantenedores robadas, gusanos que se auto-publican), typosquatting en PyPI y
vulnerabilidades de RCE en React Server Components. El vector típico es un script `postinstall` que se ejecuta al
instalar, o una versión maliciosa nueva que entra por un rango como `^1.2.0`.

**En el proyecto:**

| Medida | Dónde |
|---|---|
| Versiones **exactas** en Python (`==`), incluidas las transitivas críticas (`pydantic`, `graphql-core`) | `services/requirements.txt` |
| Versiones exactas en npm + **lockfile** con hash de integridad de cada paquete | `frontend/package.json`, `frontend/package-lock.json` |
| `npm ci` (instala solo lo del lockfile) con **`--ignore-scripts`** (ningún paquete ejecuta código al instalar) | `frontend/Dockerfile` |
| Imágenes base con tag fijo (`python:3.12-slim`, `node:20-alpine`, `nginx:1.27-alpine`) | Dockerfiles |
| **Dependabot:** las actualizaciones llegan como PR y pasan por los 6 checks del CI | `.github/dependabot.yml` |
| Multi-stage: la imagen final del frontend no lleva Node ni `node_modules` | `frontend/Dockerfile` |
| React 18 como SPA, **sin Server Components** → no expuesto a los RCE de RSC | `frontend/` |

*Lección práctica de este mismo proyecto:* como `pydantic` y `graphql-core` no estaban fijadas, un `pip install`
nuevo traía versiones incompatibles y el gateway no arrancaba. Fijar versiones protege de ataques y también de
roturas.

### 25. Seguridad multimodal en IA (esteganografía en PNG, jailbreaks)

**Concepto:** instrucciones ocultas dentro de imágenes (texto en metadatos, píxeles casi invisibles, esteganografía)
que un modelo multimodal lee y obedece: *prompt injection* por un canal que no es texto. Se mitiga tratando todo
contenido externo como **datos, nunca como instrucciones**, limpiando metadatos, re-codificando las imágenes y no
dando herramientas peligrosas al modelo que procesa contenido no confiable.

**En el proyecto: no aplica.** AegisVolt no usa modelos de IA ni recibe archivos o imágenes. La única entrada es
GraphQL tipado y validado.

### 26. Seguridad de agentes de IA (límites de recursividad, sandboxing)

**Concepto:** un agente que llama herramientas en bucle puede, por error, generar miles de peticiones (un DDoS
accidental) o ejecutar acciones destructivas. Se controla con límites de pasos/recursión, presupuestos, sandbox y
aprobación humana para acciones irreversibles.

**En el proyecto no hay agentes**, pero el **mismo principio** protege a los microservicios de sus propios clientes:
- `MAX_RETRIES = 3` con backoff → un cliente no puede entrar en un bucle infinito de reintentos (*retry storm*).
- El **circuit breaker** corta las llamadas automáticas cuando el servicio ya está caído.
- El **bulkhead** acota cuántas llamadas concurrentes puede generar el gateway.
- Los límites de GraphQL (profundidad, tokens, alias) acotan cuánto trabajo puede pedir un cliente, sea humano o
  agente.
- *Sandboxing:* contenedores sin root, con límites de memoria y en namespaces aislados por ambiente.
