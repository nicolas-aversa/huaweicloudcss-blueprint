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

2. **Configurá el entorno**:
   ```bash
   cp .env.hosted.example .env.hosted
   # editá .env.hosted:
   #   APP_DOMAIN=accel.tudominio.com
   #   APP_SECRET_KEY=$(openssl rand -hex 32)
   #   SA_ALLOWLIST=vos@huawei.com, colega@huawei.com
   ```

3. **Levantá** (Caddy resuelve el certificado TLS solo):
   ```bash
   docker compose -f docker-compose.hosted.yml --env-file .env.hosted up --build -d
   ```

4. **Entrá** a `https://accel.tudominio.com`. Cada SA:
   - hace login con su email (de la allowlist) y una contraseña,
   - va a **⚙ Configuración** y carga **su** cuenta Huawei (VPC/subnet/SG/AZ,
     project id, región, bucket) y **su** MaaS API key — o, si van a usar la cuenta
     del owner, cargan esos mismos datos del owner,
   - usa la plataforma normalmente; sus deploys van a **su** state aislado.

## Operación

- **Agregar/quitar SAs**: editá `SA_ALLOWLIST` en `.env.hosted` y recreá el
  contenedor `app`. Quitar un email de la allowlist le corta el acceso.
- **Admins**: los emails en `SA_ADMINS` (subset de la allowlist) ven en ⚙
  Configuración una card **Administración**: lista de usuarios con **"Resetear
  contraseña"** (borra la credencial → el usuario fija una nueva en su próximo
  ingreso) y el **audit log** (quién hizo qué). Es el "olvidé mi contraseña":
  el usuario avisa, un admin lo resetea.
- **Login**: es un overlay sobre la app (la app se ve blureada detrás). El primer
  login de cada SA crea su cuenta con la contraseña que elija.
- **Backups**: todo el estado por-usuario vive en el volumen `appdata`
  (`/app/data`). Respaldalo (incluye states de Terraform y credenciales).
- **Costos**: si los SAs despliegan en **su** cuenta, el costo es de ellos. Si usan
  la del **owner**, cada deploy crea clusters CSS (caros) — coordiná y destruí los
  entornos de demo cuando no se usen.

## Seguridad de las credenciales

- El `platform_settings.json` por-usuario (MaaS key + OBS AK/SK) se guarda **cifrado
  en reposo** con Fernet (AES-CBC + HMAC), clave derivada de `APP_SECRET_KEY`. Los
  archivos planos previos se migran a cifrado en el próximo guardado. Si perdés
  `APP_SECRET_KEY`, esos settings dejan de ser legibles (guardá el secreto).
- Aun así, para el modo "cuenta del owner" conviene una **AK/SK dedicada y
  revocable** en vez de tu key principal.

## Pendiente (hardening, fase 2)

- Cola de jobs para los `terraform apply` largos, backend de state remoto (OBS),
  guardrails de costo y audit log. Ver el plan.
