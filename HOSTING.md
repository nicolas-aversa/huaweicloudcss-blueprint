# Hostear la app para otros SAs (multi-usuario)

Por defecto la app corre **single-user** (un operador, su cuenta) sin login. Este
modo **hosteado** la pone detrás de una URL con **HTTPS** y **login por SA**, y le
da a cada SA su **workspace aislado**: su propia configuración de cuenta Huawei /
MaaS y su propio estado de Terraform. Dos SAs pueden desplegar a la vez, cada uno
en **su** cuenta (o en la del owner), sin pisarse.

> El login se activa **solo** si está la env var `APP_SECRET_KEY`. Sin ella, todo
> sigue funcionando single-user como siempre (dev local, tests).

## Cómo funciona el aislamiento

- **Auth**: email + password por SA (`auth.py`). Los emails permitidos salen de
  `SA_ALLOWLIST`; el primer login de cada uno crea su cuenta (password hasheado
  con pbkdf2). La sesión es una cookie firmada (HMAC), `Secure`+`HttpOnly`.
- **Workspace por usuario**: bajo `APP_DATA_DIR/users/<id>/` viven su
  `platform_settings.json` (cuenta Huawei + MaaS key) y su `terraform/` (config +
  **estado** separado). El middleware liga ese contexto en cada request.
- **Lock por usuario**: un mismo SA no puede lanzar dos deploys/destroys a la vez
  (protege su state); SAs distintos corren en paralelo.

## Pasos

1. **Una VM** (ECS de tu cuenta Huawei, u otra) con Docker + Docker Compose,
   **puertos 80 y 443 abiertos**, y un **dominio** cuyo DNS apunte a su IP pública.

2. **Levantá — HTTPS automático, zero-config** (no hace falta cargar ningún `.env`):
   ```bash
   ./hosted-up.sh
   ```
   - Detecta la **EIP** y sirve por **HTTPS** en `https://<EIP>.sslip.io/` (cert real de
     Let's Encrypt, **sin dominio propio**). Requiere 80/443 abiertos.
   - **Auth queda activa**: autogenera `APP_SECRET_KEY` y la persiste en el volumen.
   - **El primer usuario que se registra queda como admin** y gestiona el resto
     (allowlist, otros admins, resets) desde 🛡 **Panel de control**.

   Alternativas: dominio propio → `export APP_DOMAIN=tudominio && ./hosted-up.sh` (DNS→IP).
   HTTP plano (sin TLS) → `docker compose -f docker-compose.hosted.yml up --build -d`.

3. **Entrá** a la URL que te imprimió el script (`https://<EIP>.sslip.io/`). Cada SA:
   - hace login con su email + una contraseña (el primer login crea su cuenta),
   - va a **⚙ Configuración** y carga **su** cuenta Huawei (VPC/subnet/SG/AZ,
     project id, región, bucket) y **su** MaaS API key — o los datos del owner si
     van a desplegar en la cuenta del owner,
   - usa la plataforma normalmente; sus deploys van a **su** state aislado.

## Qué es por-usuario y qué es de la instancia

| | Alcance | Dónde vive |
|---|---|---|
| Cuenta Huawei / MaaS key / OBS AK-SK | **por usuario** (cifrado) | `data/users/<id>/platform_settings.json` |
| Estado de Terraform + pipelines | **por usuario** | `data/users/<id>/terraform/` |
| Historial de **Actividad** | **por usuario** | `data/users/<id>/runs/` |
| **Casos de demo creados** | **compartidos por instancia** | `data/cases/` |
| Allowlist, usuarios, audit log | instancia | `data/` |

Los **casos creados** desde el Builder los ve y despliega cualquier usuario logueado de esa VM;
borrarlos queda limitado al creador o a un admin. Como el dataset se sube al bucket de **cada
uno**, un usuario que quiera desplegar un caso que creó otro tiene que tocar **Preparar bucket**
una vez. Un caso con dataset **nunca** guarda el bucket ni las credenciales de quien lo creó — el
input se arma con las del que despliega. Los casos `live` (Kafka/JDBC) sí guardan la conexión de la
fuente, con las credenciales **cifradas** (Fernet, misma clave que el settings file).

## Operación

- **Agregar/quitar SAs (desde la app)**: los admins manejan la allowlist desde
  🛡 **Panel de control** → "Usuarios autorizados": agregan/quitan emails **sin tocar
  `.env` ni recrear el contenedor**. `SA_ALLOWLIST` (env) sigue siendo la base
  no-removible; lo agregado por UI se guarda en el volumen.
- **Quién es admin**, en este orden:
  1. los emails de `SA_ADMINS` (env), si está seteada;
  2. **más** los promovidos desde el Panel de control (`data/admins.json`);
  3. si no hay ninguno de los dos: **el primer usuario registrado** — así una instancia
     recién creada funciona sin configurar nada.

  Promover a alguien lo agrega también a la allowlist, y **nunca** se puede quitar al
  último admin (sería un lockout irreversible desde la UI). Los de `SA_ADMINS` solo se
  sacan desde el entorno.
- **Panel de control** (solo admins): allowlist, administradores, cuentas creadas con
  **"Resetear contraseña"** (vacía la credencial → el usuario fija una nueva en su
  próximo ingreso, **sin perder su rol**) y el **audit log**. Es el "olvidé mi
  contraseña": el usuario avisa, un admin lo resetea.
- **¿Nadie ve el Panel de control?** Pasa si el bootstrap le tocó a una cuenta que ya no
  usás. Miralo con `docker compose -f docker-compose.hosted.yml exec app cat
  /app/data/users.json` (el `created` más chico manda) y desbloqueate poniendo
  `SA_ADMINS=tu.email@huawei.com` en `.env.hosted` + `./hosted-up.sh`.
- **Login**: es un overlay sobre la app (la app se ve blureada detrás). El primer
  login de cada SA crea su cuenta con la contraseña que elija.
- **Backups**: todo el estado por-usuario vive en el volumen `appdata`
  (`/app/data`). Respaldalo (incluye states de Terraform, credenciales, los casos
  creados con sus datasets y el historial de Actividad).
- **Diagnóstico**: la vista **Actividad** (por usuario) guarda las últimas 50 ejecuciones
  (30 días) con sus sub-pasos y la salida cruda de Terraform. Es el primer lugar donde
  mirar cuando un deploy "salió bien" pero el cluster quedó sin datos. El audit log
  (admin) sigue siendo el "quién hizo qué".
- **Costos**: si los SAs despliegan en **su** cuenta, el costo es de ellos. Si usan
  la del **owner**, cada deploy crea clusters CSS (caros) — coordiná y destruí los
  entornos de demo cuando no se usen.
- **Guardrails de costo** (env, opcionales):
  - `MAX_PIPELINES_PER_USER` (default **0 = sin límite**): tope duro opcional. Sin
    tope, la plataforma **escala el flavor** del cluster y **reparte los workers** de
    Logstash entre las pipelines (más pipelines → menos workers c/u, mín 1) para no
    sobre-suscribir el nodo — ver `_capacity_for`.
  - `ENV_TTL_HOURS` (default 0 = off): auto-destruye entornos demo más viejos que N
    horas (usa las creds guardadas del deploy). Útil para demos efímeras; **destruye
    infra sola**, así que activalo con criterio. Chequeo cada `ENV_TTL_CHECK_SECONDS`.
  - Cada deploy/destroy queda en el **audit log** (🛡 Panel de control).

## Seguridad de las credenciales

- El `platform_settings.json` por-usuario (MaaS key + OBS AK/SK) se guarda **cifrado
  en reposo** con Fernet (AES-CBC + HMAC), clave derivada de `APP_SECRET_KEY`. Los
  archivos planos previos se migran a cifrado en el próximo guardado. Si perdés
  `APP_SECRET_KEY`, esos settings dejan de ser legibles (guardá el secreto).
- Aun así, para el modo "cuenta del owner" conviene una **AK/SK dedicada y
  revocable** en vez de tu key principal.

## Estado

- ✅ **Cola de jobs**: el deploy corre en background y sobrevive un refresh del browser.
- ✅ **Guardrails de costo**: cap de pipelines configurable + reaper TTL opcional.
- ✅ **Audit log**: quién hizo qué, en el 🛡 Panel de control.
- ✅ **Casos creados desde la app** (`custom_cases.py`): el Builder guarda el log de un
  cliente como un caso más del grid, sin tocar código ni rebuildear la imagen.
- ✅ **Actividad** (`runs.py`): historial persistido de cada ejecución, con la salida
  cruda de Terraform y un sub-paso por acción.
- ⏳ **Estado de Terraform remoto en OBS**: pendiente (hoy el state vive en el volumen
  persistente `appdata`; para durabilidad extra, respaldá ese volumen).
- ⚠️ **Input JDBC**: la config se genera bien pero el deploy falla — el plugin necesita el
  `.jar` del driver en el nodo y CSS no da acceso al filesystem. Ídem el truststore
  `.jks` de Kafka SASL_SSL. Ver `docs/guides/custom-builder-kafka-beats.md`.
