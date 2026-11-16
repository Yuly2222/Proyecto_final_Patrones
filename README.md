# AegisVolt — Plataforma de gestión de carga para flotas eléctricas

Proyecto final de **Patrones Arquitectónicos Avanzados**. Este README explica **qué se construyó, dónde está cada
cosa, qué tienes que hacer tú (cuentas, instalaciones, configuración) y cómo responder cada pregunta de la rúbrica**.

> **Modo elegido: híbrido.** Lo que el profesor puede inspeccionar en AWS es **real** (bucket S3 de estados con
> versionamiento, VPC + subredes + VPC Endpoints, DynamoDB, CloudWatch Logs, API Gateway privado).
> Kubernetes corre **en tu PC** (Docker Desktop) en lugar de EKS, para no pagar ~75 USD/mes por el clúster.
> Ver [Limitaciones honestas](#9-limitaciones-honestas-y-cómo-defenderlas).

---

## Índice
1. [Arquitectura](#1-arquitectura)
2. [Mapa del repositorio](#2-mapa-del-repositorio-qué-requisito-cubre-cada-archivo)
3. [**Lo que TÚ tienes que hacer** (checklist)](#3-lo-que-tú-tienes-que-hacer-checklist-en-orden)
4. [Correr todo en local sin AWS](#4-correr-todo-en-local-sin-aws-para-desarrollar)
5. [Cómo funciona el flujo SAGA](#5-cómo-funciona-la-saga-reservar-y-activar-carga)
6. [Rúbrica capa por capa + respuestas a las preguntas del profesor](#6-rúbrica-capa-por-capa-y-respuestas-a-las-preguntas-del-profesor)
7. [Guion de demo para la sustentación](#7-guion-de-demo-para-la-sustentación)
8. [Costos y limpieza](#8-costos-y-limpieza)
9. [Limitaciones honestas](#9-limitaciones-honestas-y-cómo-defenderlas)

---

## 1. Arquitectura

```
 Navegador ──HTTP──► frontend (nginx, 1 por ambiente)  localhost:30080 dev | 30081 test | 30082 prod
                          │ /graphql (mismo namespace)
 ┌────────────────────────▼──────────── Kubernetes (Docker Desktop) ─────────────────────────┐
 │  namespaces: aegisvolt-dev | aegisvolt-test | aegisvolt-prod                              │
 │                                                                                           │
 │                    graphql-gateway  (GraphQL Aggregation Layer + orquestador SAGA)        │
 │                 ┌──────────────┼───────────────────┐                                      │
 │                 ▼              ▼                   ▼                                      │
 │  station-reservation-svc  billing-wallet-svc  energy-dispatch-svc     (stable x4 + canary)│
 └─────────┼──────────────────────┼───────────────────┼──────────────────────────────────────┘
           ▼                      ▼                   ▼                      AWS (Terraform)
 aegisvolt-reservations-<env>  aegisvolt-billing-<env>  aegisvolt-dispatch-<env>   DynamoDB
 aegisvolt-sagas-<env> (saga log)   aegisvolt-flags-<env> (feature flags)
 CloudWatch Logs /aegisvolt/<env>/<servicio>          S3 aegisvolt-tfstate-<cuenta> (versionado)
 VPC 10.{10,20,30}.0.0/16 + VPCE (S3, DynamoDB, execute-api, logs) + API Gateway PRIVATE

 Prefect (localhost:4200) ──► salud de los 3 ambientes + auditoría SAGA en DynamoDB
```

**Stack:** Python 3.12 (FastAPI + Strawberry GraphQL + boto3), React (Vite) + nginx, Docker, Kubernetes,
Terraform ≥ 1.10, GitHub Actions, CloudWatch, Prefect 3.

---

## 2. Mapa del repositorio (qué requisito cubre cada archivo)

| Archivo / carpeta | Qué hace | Requisito del PDF |
|---|---|---|
| `services/reservation/main.py` | Reserva/libera conectores con escrituras **condicionales** en DynamoDB | 2.2.a, SAGA paso 1 y su compensación |
| `services/billing/main.py` | Cobro y reembolso con **transacciones DynamoDB**; precio con surge | 2.2.b, SAGA paso 2 y compensación, flag `ENABLE_DYNAMIC_SURGE_PRICING` |
| `services/dispatch/main.py` | Activa el cargador, telemetría simulada | 2.2.c, SAGA paso 3, flags `SIMULATE_CHARGER_HARDWARE_FAULT` y `ENABLE_FAST_CHARGE_BOOST` |
| `services/gateway/main.py` | **Esquema GraphQL** único (queries + mutations) | 2.2 API GraphQL |
| `services/gateway/saga.py` | **Orquestador SAGA** con compensaciones en orden inverso y reintentos | 2.3 |
| `services/common.py` | Logs JSON con `correlation_id` → CloudWatch; lectura de flags por ambiente | 2.5, 2.2 flags |
| `services/Dockerfile` | Una imagen Docker por microservicio (`--build-arg SERVICE=...`) | 2.2 Docker |
| `services/tests/` | `test_unit_saga.py`, `test_integration.py`, `test_e2e.py` | 3.3 b, c, d |
| `infra/bootstrap/` | Bucket S3 de estados con **Versioning Enabled** | 2.4 S3 backups |
| `infra/main.tf` | Backend S3 + **workspaces** + guardia de workspace | 2.4 workspaces |
| `infra/modules/network/` | VPC, subredes públicas/privadas, tablas de ruteo, **VPCE** | 2.4 VPC/VPCE |
| `infra/modules/dynamodb/` | 5 tablas por ambiente (Database-per-Service) + datos semilla + flags por ambiente | 2.4 DynamoDB, 2.2 flags |
| `infra/modules/observability/` | Log groups `/aegisvolt/<env>/<svc>` + métricas de errores | 2.5 CloudWatch |
| `infra/modules/apigateway/` | API Gateway REST **PRIVATE** restringido a la VPC | 2.4 APIGW |
| `k8s/` | Plantillas de Deployment/Service (stable + canary) y frontend | 2.2 Kubernetes, 3.3 canary |
| `scripts/deploy.sh` | **Canary 20 %** → health checks + CloudWatch → promover o rollback | 3.3 post-merge b |
| `.github/workflows/pr-checks.yml` | lint/build, unit, integration, e2e, terraform plan, guardia de promoción | 3.1 c, 3.2, 3.3 pre-merge |
| `.github/workflows/deploy.yml` | `terraform apply` en el workspace de la rama + canary | 3.3 post-merge a |
| `observability/flows.py` | Flows/Tasks de **Prefect**: salud + auditoría SAGA + artefactos | 2.5 Prefect |
| `frontend/` | React: estaciones en tiempo real, reserva, flags, historial SAGA | 2.1 |
| `docker-compose.yml` | Todo el stack con dynamodb-local (desarrollo y job e2e) | — |

---

## 3. Lo que TÚ tienes que hacer (checklist en orden)

Marca cada paso. Los comandos son para **PowerShell** salvo que diga *Git Bash*.

### 3.1 Instalar herramientas (≈ 20 min)

En tu PC **faltan** Python, Terraform, AWS CLI y GitHub CLI (lo verifiqué). Docker y kubectl ya están.

```powershell
winget install Python.Python.3.12
winget install Hashicorp.Terraform
winget install Amazon.AWSCLI
winget install GitHub.cli
```

- Cierra y abre la terminal después de instalar.
- Si `python` abre la Microsoft Store: *Configuración → Aplicaciones → Configuración avanzada de aplicaciones →
  Alias de ejecución de aplicaciones* → apaga los dos de `python.exe` / `python3.exe`.
- **Docker Desktop**: ábrelo (estaba apagado). *Settings → Kubernetes → Enable Kubernetes*, y en
  *Cluster provisioning method* elige **Kubeadm** (con "kind" las imágenes que construyes no se ven en el clúster).
  Apply & restart. Verifica: `kubectl config get-contexts` debe mostrar `docker-desktop`.
- Dale a Docker Desktop **al menos 6 GB de RAM** (*Settings → Resources*): son 3 ambientes × 17 pods.

### 3.2 Cuenta de AWS (≈ 15 min)

1. Crea una cuenta en https://aws.amazon.com (pide tarjeta; todo lo de este proyecto cae en capa gratuita
   excepto los Interface Endpoints, ver [costos](#8-costos-y-limpieza)).
2. **Alerta de presupuesto** (muy recomendado): *Billing → Budgets → Create budget → "Zero spend budget"* o uno de 5 USD.
3. Crea un usuario IAM para Terraform/CI: *IAM → Users → Create user* → nombre `aegisvolt-ci` → *Attach policies directly*:
   - `AmazonS3FullAccess`
   - `AmazonDynamoDBFullAccess`
   - `CloudWatchLogsFullAccess`
   - `AmazonVPCFullAccess`
   - `AmazonAPIGatewayAdministrator`
4. En el usuario → *Security credentials → Create access key → "Command Line Interface"* → **guarda** el
   Access Key ID y el Secret (solo se muestran una vez). **Nunca los subas al repo.**
5. Configura tu PC:
   ```powershell
   aws configure        # pega las llaves, región: us-east-1, formato: json
   aws sts get-caller-identity   # anota el "Account" (12 dígitos) -> lo llamaremos <ACCOUNT_ID>
   ```

> Única excepción a "nada creado por consola": este usuario IAM. Es inevitable (Terraform necesita credenciales
> para poder crear lo demás). Dilo así en la sustentación.

### 3.3 Crear el bucket de estados (una sola vez)

```powershell
cd infra/bootstrap
terraform init
terraform apply          # escribe "yes"; muestra state_bucket = aegisvolt-tfstate-<ACCOUNT_ID>
cd ../..
terraform fmt -recursive infra   # alinea el formato (el CI corre "terraform fmt -check")
```

### 3.4 Repositorio en GitHub (≈ 20 min)

1. **Reglas de rama en repo privado requieren GitHub Pro.** O haces el repo **público**
   (*Settings → General → Danger Zone → Change visibility*) o activas **GitHub Student Developer Pack**
   (https://education.github.com/pack, Pro gratis para estudiantes).
2. **Secrets** — *Settings → Secrets and variables → Actions → New repository secret*:

   | Nombre | Valor |
   |---|---|
   | `AWS_ACCESS_KEY_ID` | la llave del paso 3.2 |
   | `AWS_SECRET_ACCESS_KEY` | el secreto del paso 3.2 |
   | `TF_STATE_BUCKET` | `aegisvolt-tfstate-<ACCOUNT_ID>` |

3. **Runner self-hosted** (para que el deploy llegue al Kubernetes de tu PC):
   *Settings → Actions → Runners → New self-hosted runner → Windows x64*. Copia los comandos que te da GitHub
   en una carpeta como `C:\actions-runner`. Cuando `config.cmd` pregunte por **labels**, escribe `aegisvolt`.
   Luego `.\run.cmd` (déjalo abierto durante la demo). Ese runner usa tu `aws`, `docker`, `kubectl` y Git Bash
   (`C:\Program Files\Git\bin\bash.exe`, ya lo tienes).
4. **Subir el código y crear las 3 ramas** (antes de activar las reglas):
   ```powershell
   git add -A
   git commit -m "AegisVolt: servicios, SAGA, IaC, CI/CD, observabilidad y frontend"
   git push origin main
   git push origin main:dev main:test main:prod
   ```
   Cada push a `dev`/`test`/`prod` dispara `deploy.yml`: crea la infraestructura del ambiente y despliega en
   tu Kubernetes. Revisa la pestaña **Actions**.
5. **Reglas de protección** — *Settings → Rules → Rulesets → New branch ruleset*:
   - Name: `ambientes-protegidos` · Enforcement: **Active** · **Bypass list: vacía** (ni siquiera tú)
   - Target branches → *Add target → Include by pattern*: `dev`, `test`, `prod`
   - ☑ **Restrict deletions**
   - ☑ **Require a pull request before merging** (Required approvals: **0**, porque la entrega es individual y GitHub
     no deja aprobar tu propio PR)
   - ☑ **Require status checks to pass** → *Add checks*: `promotion-guard`, `lint-build`, `unit-tests`,
     `integration-tests`, `e2e-tests`, `terraform-plan` (aparecen después de que corran una vez en un PR;
     si no salen, abre un PR de prueba primero y vuelve)
   - ☑ **Block force pushes**
   - Save.
6. **Primer PR de evidencia**: `git checkout dev; git pull; git checkout -b feature/readme-demo`, cambia algo,
   push, abre PR `feature/readme-demo → dev`, espera los 6 checks en verde, merge. Luego PR `dev → test` y
   `test → prod`. Esto deja en el historial la cadena de promoción completa que el profesor va a revisar.

### 3.5 Prefect (≈ 5 min)

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r observability/requirements.txt
prefect server start                     # terminal 1 -> http://localhost:4200
# terminal 2 (con el venv activado):
$env:PREFECT_API_URL="http://localhost:4200/api"
python observability/flows.py            # corre el flow cada 5 min; también puedes darle "Run" desde la UI
```

### 3.6 Información que necesito de ti si algo falla

Si me pides ayuda con un error, pégame: el **nombre del job** de Actions y las últimas ~40 líneas del log, o la
salida de `kubectl -n aegisvolt-dev get pods` / `kubectl -n aegisvolt-dev logs deploy/gateway-stable`.
**Nunca me pegues las llaves de AWS.**

---

## 4. Correr todo en local sin AWS (para desarrollar)

```powershell
docker compose up -d --build        # http://localhost:8080  (ambiente "ci", dynamodb-local)
```

Tests:

```powershell
pip install -r services/requirements-dev.txt
pytest services/tests/test_unit_saga.py                                  # unitarios
$env:DYNAMODB_ENDPOINT="http://localhost:8000"; pytest services/tests/test_integration.py   # integración
$env:E2E_URL="http://localhost:8080"; pytest services/tests/test_e2e.py                     # e2e
```

> El test de integración **borra y recrea** las tablas `aegisvolt-*-ci` de dynamodb-local.

---

## 5. Cómo funciona la SAGA "Reservar y Activar Carga"

**Tipo: Orquestación** (un coordinador central, `services/gateway/saga.py`). Se eligió sobre coreografía
porque el flujo es lineal, con un dueño claro, y el estado completo queda en un solo lugar (`aegisvolt-sagas-<env>`)
que Prefect puede auditar.

```
mutation reserveAndCharge ──► saga_id (= correlation_id) ──► aegisvolt-sagas-<env>: STARTED
  1. reservation /reserve   conector AVAILABLE -> RESERVED            falla -> FAILED (nada que deshacer)
  2. billing     /charge    saldo -= monto  (+ TXN#<saga>#CHARGE)     falla -> release            -> COMPENSATED
  3. dispatch    /start     cargador ACTIVE                           falla -> refund -> release  -> COMPENSATED
  4. reservation /confirm   RESERVED -> CHARGING                      -> COMPLETED
```

- **Compensaciones en orden inverso**: primero reembolso (billing), después liberar el conector (reservation).
- **Reintentos**: cada compensación se intenta hasta 3 veces. Si aun así falla → `COMPENSATION_FAILED`
  (Prefect lo reporta).
- **Idempotencia** (pregunta segura en la sustentación):

  | Operación | Cómo se garantiza que hacerla 2 veces = hacerla 1 vez |
  |---|---|
  | `reserve` | `ConditionExpression: status = AVAILABLE OR saga_id = :mi_saga` |
  | `charge` | Transacción DynamoDB: `Put TXN#<saga>#CHARGE` con `attribute_not_exists(pk)` **y** descuento en la misma transacción. Si el TXN ya existe, la transacción entera se cancela → no hay doble cobro |
  | `refund` | Igual, con `TXN#<saga>#REFUND`: solo puede existir uno → no hay doble reembolso |
  | `release` | `ConditionExpression: saga_id = :mi_saga` → si ya se liberó, responde `ALREADY_RELEASED` sin tocar nada |
  | `start` | `attribute_not_exists(session_id)` con `session_id = saga_id` |

  La llave de idempotencia es el **`saga_id`**, y se aplica **en la base de datos** (escrituras condicionales
  atómicas), no en memoria, así que funciona aunque haya varias réplicas o el pod se reinicie.
  Lo prueba `test_compensations_are_idempotent`.

---

## 6. Rúbrica capa por capa y respuestas a las preguntas del profesor

La nota es **binaria por pregunta**. Abajo, cada pregunta del banco del PDF con la respuesta y la demo.

### Capa 1 — IaC, S3, Redes (30 %)

**¿Cómo garantiza que `terraform apply` en `test` no sobrescriba DynamoDB ni el estado de `prod`?**
1. **Estado separado**: con workspaces, el backend S3 guarda cada ambiente en su propia llave:
   `env:/test/aegisvolt/terraform.tfstate` vs `env:/prod/aegisvolt/terraform.tfstate`. El apply de test ni
   siquiera lee el estado de prod.
2. **Nombres separados**: todo usa `terraform.workspace` (`infra/main.tf` → `local.env`): tabla
   `aegisvolt-billing-test` ≠ `aegisvolt-billing-prod`, VPC `10.20.0.0/16` ≠ `10.30.0.0/16`.
3. **El CI elige el workspace por la rama**, no una persona (`deploy.yml`: `terraform workspace select "$ENVIRONMENT"`).
4. **Guardia**: `terraform_data.workspace_guard` falla si el workspace no es dev/test/prod (evita aplicar en `default`).
5. **Bloqueo** `use_lockfile = true`: dos applies simultáneos al mismo estado se bloquean.
6. En prod las tablas tienen `deletion_protection_enabled` y Point-in-Time Recovery.

**Demo — versiones del tfstate de dev y cómo restaurar:**
```powershell
aws s3api list-object-versions --bucket aegisvolt-tfstate-<ACCOUNT_ID> --prefix "env:/dev/aegisvolt/terraform.tfstate" --query "Versions[].[VersionId,LastModified,IsLatest]" --output table
```
(En consola: S3 → bucket → carpeta `env:/dev/aegisvolt/` → activar **Show versions**.)
Restaurar una versión previa (se copia encima y queda como la más reciente; no se pierde ninguna):
```powershell
aws s3api copy-object --bucket aegisvolt-tfstate-<ACCOUNT_ID> --key "env:/dev/aegisvolt/terraform.tfstate" --copy-source "aegisvolt-tfstate-<ACCOUNT_ID>/env:/dev/aegisvolt/terraform.tfstate?versionId=<VERSION_ID>"
cd infra; terraform workspace select dev; terraform plan    # verificar que el estado restaurado cuadra con la realidad
```

**Recorrido de un paquete API Gateway → VPCE → pod, y qué tabla de ruteo / política interviene**
(`infra/modules/network`, `infra/modules/apigateway`):
1. Un cliente **dentro de la VPC** resuelve `https://<api-id>.execute-api.us-east-1.amazonaws.com`. Como el
   Interface Endpoint `execute-api` tiene **private DNS**, el nombre resuelve a las IPs privadas de sus ENI en
   las subredes privadas.
2. **Security Group `aegisvolt-<env>-vpce-sg`** del endpoint: solo acepta TCP 443 desde el CIDR de la VPC.
3. El API es tipo **PRIVATE**; su **resource policy** niega cualquier invocación cuyo `aws:SourceVpc` no sea esta VPC.
4. La integración `/graphql` (HTTP_PROXY) iría por un **VPC Link** a un **NLB interno** en las subredes privadas
   → NodePort del nodo (SG `k8s-nodes-sg`: 30000-32767 solo desde la VPC) → kube-proxy → Service `gateway` → pod.
5. **Tabla de ruteo privada** `aegisvolt-<env>-private-rt`: solo tiene la ruta `local` y las prefix-lists que
   inyectan los **Gateway Endpoints** de S3 y DynamoDB. **No tiene `0.0.0.0/0`** (ni NAT ni IGW), así que
   nada sale a internet: pod → DynamoDB va por el Gateway Endpoint; pod → CloudWatch por el Interface Endpoint `logs`.

Demo: `cd infra; terraform workspace select dev; terraform output` y en consola *VPC → Endpoints* / *Route tables*.

### Capa 2 — CI/CD, Git y Canary (25 %)

**Demo — intentar `git push --force` o push directo a `test`:**
```powershell
git checkout test; git commit --allow-empty -m "push directo"; git push origin test
git push --force origin test
```
Respuesta esperada de GitHub: `GH013: Repository rule violations found for refs/heads/test` con
*"Changes must be made through a pull request"* y *"Cannot force-push to this branch"*. La regla es el ruleset
`ambientes-protegidos` (paso 3.4.5). Después: `git reset --hard origin/test` para limpiar.

**¿Cómo se determina en el YAML el ambiente del `terraform apply`?** — `.github/workflows/deploy.yml`:
el evento es `push` a `dev|test|prod` (solo puede venir de un merge, porque el push directo está bloqueado).
`ENVIRONMENT: ${{ github.ref_name }}` es el nombre de la rama → `terraform workspace select -or-create "$ENVIRONMENT"`
→ `terraform apply -auto-approve`. En el PR (`pr-checks.yml`) se usa `github.base_ref` (rama **destino**) para el `plan`.

**Promoción estricta:** el job `promotion-guard` rechaza cualquier PR que no sea `feature/*|fix/* → dev`,
`dev → test` o `test → prod`. Como es check obligatorio, un PR `feature/x → prod` no se puede fusionar.

**¿Qué evalúa el canary antes de pasar de 20 % a 100 %?** — `scripts/deploy.sh`, función `canary_healthy`:
- Se crea `<svc>-canary` con **1 réplica** junto a `<svc>-stable` con **4**. El Service selecciona solo por
  `app=<svc>`, así que reparte ~**20 %** al canary.
- Se envían 40 consultas GraphQL que tocan los 4 servicios. Condiciones, **todas** deben cumplirse:
  1. `kubectl rollout status` del canary OK (readiness probe `/health`),
  2. **0 reinicios** del pod canary,
  3. **0 respuestas no-200**,
  4. **errores `level=ERROR` en CloudWatch** del stream del canary (`--log-stream-name-prefix <svc>-canary`) ≤ `ERROR_THRESHOLD` (0).
- Pasa → stable se actualiza a la nueva imagen (100 %) y se borra el canary. Falla → se borra el canary
  (**rollback automático**: stable nunca se tocó) y el job termina en rojo.
- La falla simulada de hardware se loguea como `WARNING`, no `ERROR`: es una falla de negocio esperada y no
  debe bloquear despliegues.

### Capa 3 — Backend, GraphQL, SAGA y Feature Flags (25 %)

**Esquema GraphQL** (`services/gateway/main.py`; el CI lo exporta como artefacto `graphql-schema`):
- Queries: `environment`, `stations`, `featureFlags`, `sagas`, `wallets`, `telemetry`
- Mutations: `reserveAndCharge(connectorId, fleetId, kwh)`, `releaseConnector`, `setFeatureFlag`

**Desacople**: 4 imágenes, 4 Deployments, comunicación solo por HTTP vía Services de Kubernetes, y cada servicio
escribe **solo en su tabla** (Database-per-Service). El gateway nunca toca la tabla de billing directamente.

**Feature flags por ambiente**: tabla `aegisvolt-flags-<env>`, valores iniciales distintos por ambiente en
`infra/modules/dynamodb/seed.tf` (surge `true` en dev/test, `false` en prod). `ignore_changes = [item]` hace que
Terraform no revierta los cambios hechos en caliente. Los servicios leen la flag en cada request (caché de 3 s),
por eso **no hace falta redesplegar**.

| Flag | Efecto |
|---|---|
| `ENABLE_DYNAMIC_SURGE_PRICING` | billing cobra ×1.5 |
| `ENABLE_FAST_CHARGE_BOOST` | dispatch entrega 350 kW en lugar de 150 kW |
| `SIMULATE_CHARGER_HARDWARE_FAULT` | dispatch responde 503 → la SAGA compensa |

**Demo — activar la falla en `test` y mostrar la compensación en DynamoDB:** ver [guion, paso 3](#7-guion-de-demo-para-la-sustentación).

**Idempotencia:** ver la tabla de la [sección 5](#5-cómo-funciona-la-saga-reservar-y-activar-carga).

### Capa 4 — Observabilidad (12 %)

**Logs**: cada línea es JSON:
`{"ts","level","env","service","correlation_id","event",...}` → grupo `/aegisvolt/<env>/<servicio>`, stream = nombre del pod.

**Rastrear el `correlation_id` de una saga fallida** — *CloudWatch → Logs Insights* → selecciona los 4 grupos
`/aegisvolt/test/*` → consulta:
```
fields @timestamp, service, level, event
| filter correlation_id = "<SAGA_ID copiado del frontend>"
| sort @timestamp asc
```
Verás la historia completa entre servicios: `saga_started → connector_reserved → wallet_charged →
charger_hardware_fault → wallet_refunded → connector_released → saga_rolled_back`.

**Prefect** (`observability/flows.py`): flow `aegisvolt-observability` con tasks `check_health` (cadena completa
frontend→gateway→servicios→DynamoDB por ambiente) y `audit_sagas` (cuenta completadas/compensadas y detecta
**huérfanas** — en curso hace > 5 min — e **inconsistentes** — compensadas con cobro pero sin reembolso).
Publica 3 artefactos (`aegisvolt-health`, `aegisvolt-saga-audit`, `aegisvolt-summary`) visibles en
*Prefect UI → Artifacts*, y el run queda en rojo si hay problemas.

### Capa 5 — Frontend (8 %)

`frontend/src/main.jsx`. Un frontend por ambiente (dev :30080, test :30081, prod :30082; color verde/naranja/rojo).
nginx reenvía `/graphql` al gateway **de su propio namespace**. Refresco cada 2 s (polling). Paneles:
(a) conectores y telemetría, (b) botón "Reservar y activar carga" (dispara la SAGA), (c) flags del ambiente con
interruptor, (d) historial de sagas; clic en una fila muestra cada paso y compensación.

---

## 7. Guion de demo para la sustentación

Antes: Docker Desktop encendido, runner corriendo, `prefect server start` + `python observability/flows.py`.

1. **Ambientes vivos**: abre http://localhost:30080, :30081 y :30082. `kubectl get pods -A | findstr aegisvolt`.
2. **Reserva exitosa (test)**: en :30081 elige `fleet-rapiexpress`, "Reservar y activar carga" → saga
   `COMPLETED`, conector `CHARGING` con telemetría.
3. **Falla inducida en test**: marca `SIMULATE_CHARGER_HARDWARE_FAULT` en :30081 (o por CLI, abajo) → reserva
   otro conector → la saga sale `COMPENSATED`; abre la fila y muestra `START_CHARGER ✘`, `COMPENSATE_CHARGE ✔ REFUNDED`,
   `COMPENSATE_RESERVE ✔ AVAILABLE`. Luego en *DynamoDB → Explore items*:
   - `aegisvolt-billing-test`: existen `TXN#<saga>#CHARGE` **y** `TXN#<saga>#REFUND`; el balance volvió al valor anterior.
   - `aegisvolt-reservations-test`: el conector está `AVAILABLE` y sin `saga_id`.
   - `aegisvolt-sagas-test`: `status = COMPENSATED` con la lista de pasos.
4. **Flag en dev sin redesplegar, prod intacto**: en :30080 desmarca `ENABLE_DYNAMIC_SURGE_PRICING` y reserva →
   el monto baja (sin ×1.5). Muestra :30082 con sus flags sin cambios. Por CLI:
   ```powershell
   aws dynamodb update-item --table-name aegisvolt-flags-dev --key file://flagkey.json --update-expression "SET enabled = :e" --expression-attribute-values file://flagval.json
   ```
   con `flagkey.json` = `{"flag":{"S":"ENABLE_DYNAMIC_SURGE_PRICING"}}` y `flagval.json` = `{":e":{"BOOL":false}}`
   (en PowerShell 5 es más fácil con archivos que con JSON en línea).
5. **CloudWatch**: Logs Insights con el `saga_id` del paso 3.
6. **Prefect**: http://localhost:4200 → run del flow → artefacto `aegisvolt-summary` muestra la saga compensada.
7. **Git**: push directo / force push a `test` rechazado; muestra los PRs `feature→dev→test→prod` con los 6 checks.
8. **S3**: versiones del tfstate de dev (comando de la capa 1).

---

## 8. Costos y limpieza

| Recurso | Costo |
|---|---|
| S3, DynamoDB on-demand, CloudWatch Logs, API Gateway, VPC, subredes, Gateway Endpoints | 0 USD (capa gratuita / sin costo) |
| **Interface Endpoints** (`execute-api`, `logs`) | ~0.01 USD/h por endpoint por AZ → ~0.04 USD/h por ambiente |

Los Interface Endpoints están **apagados por defecto** (`enable_interface_endpoints = false`). Para la
sustentación, cámbialo a `true` en `infra/variables.tf` mediante un PR `feature/vpce-on → dev → test → prod`
(¡sirve además como demo de la promoción!) y regrésalo a `false` después.

Destruir todo al terminar el curso (en cada workspace; para prod primero pon `deletion_protection_enabled = false`
y aplica):
```powershell
cd infra; terraform workspace select dev; terraform destroy
```

---

## 9. Limitaciones honestas y cómo defenderlas

- **Kubernetes local en vez de EKS**: el PDF permite emulación local. Los manifiestos, probes, Services, canary y
  namespaces son idénticos a EKS; en EKS cambiaría el contexto de `kubectl` y se agregaría el VPC Link al NLB.
- **API Gateway privado sin backend alcanzable**: está creado y protegido por la VPC, pero su integración apunta
  a un nombre interno porque el clúster vive en tu PC. El tráfico real del navegador entra por el nginx del frontend.
  El recorrido de red de la pregunta 6.1 se explica con los recursos reales (VPCE, SG, tablas de ruteo, resource policy).
- **"Tiempo real" por polling cada 2 s**, no WebSockets/subscriptions.
- **Credenciales**: los pods usan las llaves del usuario IAM vía Secret de Kubernetes. En EKS se usaría IRSA
  (rol IAM por ServiceAccount) y no habría llaves.
- **No verificado aún**: este código se escribió sin poder ejecutarse en tu PC (faltaban Python, Terraform y
  Docker estaba apagado). La primera corrida de los checks en un PR es la verificación real.
