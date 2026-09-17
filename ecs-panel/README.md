# Panel de la ECS

Webapp mínima para **prender y apagar desde el celular** la ECS que hostea la plataforma, sin entrar
a la consola de Huawei Cloud.

## Los puertos no se tocan

Una versión anterior abría el 80/443 en el security group al encender y los cerraba al apagar. Se
fue: **una ECS apagada no responde a nada, con los puertos como estén**, así que cerrarlos no
protegía nada. Y el resto del repo (`hosted-up.sh`, `HOSTING.md`) ya pide esos puertos abiertos
como requisito permanente de la máquina — el panel los cerraba contra su propia documentación.

Lo que sí costaba: cada consulta de estado eran dos llamadas (ECS + VPC) en vez de una, la agency
necesitaba permisos de VPC, y existía el estado "apagada + puertos abiertos" con toda una lógica
para salir de él. Los puertos se abren **una vez** en el SG y se quedan.

> Si venís de la versión anterior: la regla que el panel había creado (description
> `ecs-panel:auto`) queda como está si la ECS estaba encendida al actualizar — ya nadie la borra.
> Si estaba **apagada**, la regla no existe: agregá a mano una regla de entrada TCP `80,443` desde
> `0.0.0.0/0` en el SG de la ECS antes del próximo encendido, o Caddy no va a poder renovar el
> certificado.

## El estado en vivo

El panel poletea cada 5 s mientras hay una transición en curso, y para cuando el estado se estabiliza.

Lo que lo hace menos obvio de lo que parece: **la API de ECS no cambia `status` durante la
transición**. Un `os-start` deja la instancia reportando `SHUTOFF` con
`OS-EXT-STS:task_state: "powering-on"` hasta que termina (y un `os-stop`, `ACTIVE` con
`powering-off`). Leyendo solo `status`, el panel veía el estado viejo 1,5 s después de apretar el
botón, lo daba por definitivo y **cortaba el polling** — se quedaba mostrando "Apagada" para
siempre, mientras la máquina arrancaba.

Hay dos redes, a propósito:

1. **`ecs_status` combina `status` con `task_state`** y devuelve `TRANSICION` si hay una operación
   en curso.
2. **El frente recuerda qué acción pediste**: un `SHUTOFF` que llega justo después de un `start` se
   trata como transición aunque el backend todavía no lo sepa.

La segunda no es redundante: **el Worker y la función se despliegan por separado**, así que el panel
tiene que aguantar hablando con una función vieja que todavía miente. Hay un test de eso.

## Por qué son dos piezas

FunctionGraph **no ofrece ninguna puerta HTTP gratis**. Las HTTP functions solo aceptan triggers de
APIG (dedicado, facturado por hora) o APIC (solo AP-Singapore), y el
[APIG compartido está dado de baja](https://support.huaweicloud.com/intl/en-us/usermanual-apig/apig-ug-180307020.html)
para cuentas nuevas. En LA-Santiago no queda ninguna opción sin costo.

Así que el frente vive afuera y la lógica adentro:

```
celular → Cloudflare Worker  ──(API de invocación)──→  FunctionGraph  ──→  ECS
          password + cookie                            agency con
          (worker.js)                                  permisos reales (index.py)
```

Partirlo así tiene una ventaja que no es accidental: **la credencial que guarda Cloudflare se
scopea a "invocar esta función" y nada más**. Si se filtra, lo máximo que consigue alguien es
prenderte y apagarte la máquina — no tocar la ECS en sí. Los permisos anchos se quedan del lado de
Huawei, en la agency.

## Qué es cada archivo

| | |
|---|---|
| `index.py` | La función de FunctionGraph: consulta el estado y prende/apaga. ~280 líneas, sin dependencias fuera de la stdlib. Reemplaza a la función de start/stop que ya tenías. |
| `worker.js` | El frente en Cloudflare: sirve la página, valida la password, invoca la función. La página usa los tokens del app (`static/index.html`, `:root`): mismas superficies, mismo rojo, misma Inter — es la card de login del app puesta sola en la pantalla. |
| `wrangler.toml` | Config del Worker. **Los secretos no van acá.** |
| `test_panel.py` | 41 tests de la función. `py -m pytest ecs-panel/ -q` desde la raíz del repo. |
| `worker.test.mjs` | 46 checks: el Worker con Web Crypto real y `fetch` interceptado, **más el JS del panel** corrido en un sandbox con timers controlados y una ECS que tarda en arrancar. Ese último bloque es el que hacía falta: el script del panel vive dentro de un template string y hasta ahora no lo ejecutaba ningún test. `node ecs-panel/worker.test.mjs` (desde esta carpeta). |

## Despliegue

### 1. La función (Huawei)

**Agency de IAM** delegada en FunctionGraph, con permisos de **ECS** (consultar, arrancar, detener).
Si ya tenías una para el encendido programado, sirve tal cual.

Crear una función Python, pegar `index.py`, handler `index.handler`, timeout 60 s.

**Red: public access, no VPC access.** La función sólo habla con la API de control de Huawei, que
es pública. Adentro de una VPC [queda aislada de
internet](https://support.huaweicloud.com/intl/en-us/functiongraph_faq/functiongraph_03_0834.html) y
necesitaría un NAT gateway sólo para volver al punto de partida; el síntoma sería un timeout en la
primera llamada, sin ninguna pista de que el problema es la red.

Configuración en Environment/User Data:

| Clave | Obligatoria | Qué es |
|---|---|---|
| `project_id` | sí | Project ID de la región. |
| `region` | sí | Ej. `la-south-2`. |
| `ecs_id` | sí | La instancia a manejar. |
| `app_url` | no | URL de la plataforma (ej. `https://<EIP>.sslip.io`), para el link "Abrir la plataforma". No se puede derivar sola: el frente vive en el dominio del Worker. |

`sg_id`, `ports`, `source_cidr` y `rule_marker` eran de la versión que manejaba puertos; si siguen
en el User Data, se ignoran.

**No hace falta ningún trigger HTTP.** El Worker la invoca por la API.

### 2. El usuario IAM para el Worker

Creá un usuario IAM **dedicado** cuyo único permiso sea invocar esa función
(`functiongraph:function:invoke`). No reutilices tu usuario ni uno con permisos anchos: la gracia de
esta arquitectura es justamente que esa credencial no pueda hacer nada más.

### 3. El Worker (Cloudflare)

**El `name` del `wrangler.toml` es la URL**: `css` → `css.naversa.workers.dev`. Con otro nombre,
`deploy` crea un Worker nuevo y el viejo sigue sirviendo lo de antes (pasó: un deploy con
`name = "ecs-panel"` publicó el rediseño y la contraseña nueva en otra URL mientras el bookmark
seguía apuntando a la vieja).

En el toml solo va `HW_REGION`. Todo lo demás se carga con `wrangler secret put` — **nunca en el
archivo**, que se versiona en un repo público:

```bash
cd ecs-panel
npx wrangler secret put PANEL_PASSWORD   # larga y aleatoria, ver "Seguridad"
npx wrangler secret put PANEL_SECRET     # otro string largo al azar, para firmar la cookie
npx wrangler secret put HW_USER          # el usuario IAM del paso 2
npx wrangler secret put HW_DOMAIN        # la cuenta (domain name) de ese usuario
npx wrangler secret put HW_PASSWORD
npx wrangler secret put HW_PROJECT_ID    # project id de la región
npx wrangler secret put FUNCTION_URN     # lo muestra la consola de FunctionGraph
npx wrangler deploy
```

`HW_PROJECT_ID` y `FUNCTION_URN` no son contraseñas, pero identifican tu cuenta y el Worker los lee
de `env` igual. Van como secrets por una razón práctica: **`wrangler deploy` reemplaza las vars del
Worker por las del toml** y borra las que estén solo en el dashboard; los secrets sobreviven a
cualquier deploy. Si ya los tenés como vars en el dashboard, desplegá con `--keep-vars` o pasalos a
secrets una vez.

La URL que imprime es la que guardás como bookmark en el celular.

### 4. El encendido programado (opcional)

Un trigger **Timer** sobre la función, con `user_event` = `<ecs_id>,startup` o
`<ecs_id>,shutdown`. Es el mismo contrato que la función anterior, así que un timer ya configurado
sigue funcionando apuntado a esta.

## Seguridad

La URL del Worker es pública: cualquiera que la adivine llega al **formulario de password**. No a
las acciones, que exigen una cookie firmada con HMAC-SHA256; sin `PANEL_SECRET` no se puede
fabricar. La cookie es `HttpOnly; Secure; SameSite=Strict` y dura 7 días, así que desde el celular
se loguea una vez y queda.

**`PANEL_PASSWORD` tiene que ser larga y aleatoria.** No hay rate limiting: sin estado compartido
entre invocaciones no hay dónde contar intentos. La comparación sí es de tiempo constante (se
comparan los SHA-256, no los strings, para que el tiempo tampoco dependa del largo).

**Para cambiar la contraseña** no hay que tocar código: es un secreto del Worker.

```bash
cd ecs-panel
npx wrangler secret put PANEL_PASSWORD    # pide el valor nuevo por consola
```

Rige al instante, sin redeploy. Las sesiones ya abiertas **siguen válidas**: la cookie se firma
con `PANEL_SECRET`, no con la contraseña. Si además querés cerrar todas las sesiones (un celular
perdido, una cookie que pudo quedar en otro navegador), rotá también `PANEL_SECRET` con el mismo
comando: todas las cookies dejan de validar y hay que volver a loguearse.

## Detalles que no son arbitrarios

- **El apagado es `SOFT`, no `HARD`** (la versión anterior usaba HARD). Un `HARD` es desenchufar la
  máquina. Aunque el estado de Terraform ahora viva en OBS, adentro siguen estando el `users.json`,
  los settings cifrados de cada SA, los casos creados y el registro de pipelines.
- **La función no sirve ninguna página ni valida contraseñas.** Eso lo hace el Worker. Lo único que
  la protege es la credencial IAM que hace falta para invocarla — por eso esa credencial se scopea a
  invocar esta función y nada más.

## Antes de confiar en el panel

**La app tiene que volver sola al encender.** Los dos servicios ya tienen `restart: unless-stopped`,
así que alcanza con que el daemon de Docker arranque en el boot:

```bash
sudo systemctl enable docker
sudo systemctl is-enabled docker    # debe decir "enabled"
sudo reboot
```

Esperá ~60 s y abrí la URL de la plataforma **sin entrar por SSH**. Si carga, el panel sirve.

La EIP no cambia al detener la ECS (sigue bindeada, y facturándose igual que el disco), así que la
URL `<EIP>.sslip.io` se mantiene y el apagado es transparente para la app.

## Verificación la primera vez

1. Abrir la URL del Worker sin sesión → pide password. Con una incorrecta → rechaza.
2. **Encender** → el panel pasa a "Encendiendo…" con el punto pulsando, sigue consultando, y en
   ~40-60 s dice "Encendida".
3. Abrir la plataforma → carga con candado verde. Si no carga, revisá que el SG de la ECS tenga
   el 80/443 abierto — el panel ya no lo hace por vos.
4. **Apagar** → "Apagando…" y después "Apagada".

## Limitación conocida

El panel **no chequea si hay un deploy en curso** antes de apagar. Un `terraform apply` dura 5-25
minutos y cortar la VM en el medio deja recursos a medio crear en Huawei. Por ahora: mirá Actividad
en la plataforma antes de apagar. El paso siguiente natural es que la función consulte
`/api/v1/terraform/jobs` y se niegue si hay uno corriendo.
