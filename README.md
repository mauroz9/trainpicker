# 🚄 Renfe Alertas Bot

Un bot de Telegram de código abierto que vigila la web de Renfe y te avisa cuando se libera un asiento en un tren que estaba completo. Ideal para usuarios de abonos de Media Distancia y Avant.

## ✨ Características

- ✅ Monitoreo automático de trenes en Renfe cada 5 segundos (configurable)
- ✅ Alertas instantáneas por Telegram cuando se libera una plaza
- ✅ Base de datos SQLite persistente para guardar alertas
- ✅ Interfaz inteligente con soporte a teclado de Telegram
- ✅ Búsqueda avanzada de trenes con horarios y disponibilidad
- ✅ Despliegue con Docker (sin dependencias de sistema)
- ✅ Arquitectura escalable con dos servicios: Bot + Scheduler

## 🚀 Cómo desplegar tu propio bot en 5 pasos

Gracias a Docker y Docker Compose, desplegar este bot es extremadamente sencillo. No necesitas instalar Python ni configurar navegadores.

### Requisitos previos:
1. Tener [Docker y Docker Compose](https://docs.docker.com/get-docker/) instalado en tu máquina o servidor.
2. Una cuenta de Telegram.
3. Acceso a GitHub o descargar este repositorio.

### Paso 1️⃣ - Crear un bot en Telegram

1. Abre Telegram y busca a [@BotFather](https://t.me/botfather)
2. Escribe `/newbot` y sigue las instrucciones para crear tu bot
3. Recibirás un **Token HTTP API** que se parece a: `809180...:AAHe5w-Q1...`
4. Guarda ese token en un lugar seguro (lo necesitarás en el paso siguiente)

### Paso 2️⃣ - Clonar y configurar el repositorio

**Clona este repositorio:**
```bash
git clone https://github.com/mauroz9/trainpicker
cd renfe-bot
```

### Paso 3️⃣ - Configurar las variables de entorno

**Copia el archivo `.env.example` a `.env`:**
```bash
cp .env.example .env
```

**Abre `.env` con tu editor favorito y reemplaza el valor de `TELEGRAM_BOT_TOKEN` con tu token:**
```env
# .env
TELEGRAM_BOT_TOKEN=809180...:AAHe5w-Q1...
```

**⚠️ Importante:** Nunca commits el archivo `.env` en Git. Ya viene en `.gitignore` por seguridad.

### Paso 4️⃣ - Iniciar los servicios con Docker Compose

**Para desplegar el bot en tu máquina local:**
```bash
docker-compose up -d
```

Esto hará lo siguiente:
- Descargará la imagen de Python 3.11
- Instalará todas las dependencias (python-telegram-bot, playwright, apscheduler, etc.)
- Compilará el navegador Chromium necesario para Playwright
- Iniciará dos servicios en contenedores separados:
  - **telegram_bot**: El bot que escucha comandos en Telegram
  - **renfe_scheduler**: El programador que vigila trenes cada 5 segundos (configurable)

**Para ver los logs en tiempo real:**
```bash
docker-compose logs -f
```

**Para detener los servicios:**
```bash
docker-compose down
```

### Paso 5️⃣ - Usar el bot en Telegram

1. Abre Telegram y busca tu bot por nombre
2. Escribe `/start` y recibirás un mensaje de bienvenida
3. Escribe `/buscar` para iniciar una búsqueda de trenes
4. Completa los pasos:
   - **Origen:** ej. "Madrid"
   - **Destino:** ej. "Barcelona"
   - **Fecha:** en formato DD/MM/AAAA (ej. "25/04/2026")
5. El bot mostrará una lista de trenes disponibles
6. Si un tren está completo, verás un botón 🔔 **Crear alerta**
7. Cuando el tren se libere, ¡recibirás una notificación automática!

## 📁 Estructura del proyecto

```
renfe-bot/
├── main.py                 # Bot principal que escucha comandos de Telegram
├── scheduler.py            # Servicio que revisa alertas cada X segundos
├── scraper.py              # Navegador automatizado que busca trenes en Renfe
├── database.py             # Gestión de alertas (SQLite)
├── docker-compose.yml      # Docker Compose para desarrollo local (build: .)
├── compose.prod.yml        # Docker Compose para producción (image: ya publicada)
├── Dockerfile              # Imagen Docker personalizada
├── scripts/
│   ├── release.sh          # Build + tag + push a GitLab Container Registry (bash)
│   └── release.ps1         # Igual que release.sh, para Windows/PowerShell
├── requirements.txt        # Dependencias Python
├── .env.example            # Plantilla de variables de entorno (desarrollo)
├── .env.prod.example       # Plantilla de variables de entorno (producción)
├── .gitignore              # Archivos ignorados en Git
└── README.md               # Este archivo
```

## 🔧 Configuración avanzada

### Cambiar intervalo de revisión de alertas

`scheduler.py` corre dos jobs independientes para que una ruta que necesite
recapturar sesión con Playwright (10-30s) no retrase la comprobación del
resto de rutas:

- `fast_check_alerts`: solo lee la sesión ya cacheada (sin abrir navegador).
  Si una ruta no tiene cache válido, se salta ese ciclo para esa ruta.
- `refresh_sessions`: recaptura con Playwright solo las rutas activas sin
  cache válido, acotado por un semáforo para no abrir demasiados navegadores
  a la vez. Si la recaptura ya trae plaza libre, notifica al instante.

Ambos intervalos (y el límite de recapturas concurrentes) se configuran por
variables de entorno en `.env` (ver `.env.example`):

```bash
# Cada cuánto se comprueba la sesión cacheada (recomendado 3s)
FAST_CHECK_INTERVAL_SECONDS=3

# Cada cuánto se recaptura con Playwright la sesión de las rutas sin cache (recomendado 20s)
SESSION_REFRESH_INTERVAL_SECONDS=20

# Máximo de recapturas con Playwright en paralelo (recomendado 2)
MAX_CONCURRENT_REFRESHES=2
```

### Usar una base de datos centralizada

Por defecto, las alertas se guardan en `data/renfe_alerts.db` dentro del contenedor. Para persistencia real:

1. Modifica el volumen en `docker-compose.yml`:
```yaml
volumes:
  - ./data:/app/data  # Guarda alertas localmente en ./data
```

2. Crea la carpeta si no existe:
```bash
mkdir data
```

## 📦 Dependencias

- **python-telegram-bot** `>=20.0`: SDK oficial de Telegram Bot API
- **playwright** `>=1.40`: Navegador automatizado (descarga Chromium)
- **apscheduler**: Programador de tareas asyncrónicas
- **python-dotenv**: Gestión de variables de entorno

## 🐛 Solución de problemas

### El bot no responde
- ✅ Verifica que el token en `.env` sea correcto
- ✅ Revisa los logs: `docker-compose logs -f telegram_bot`
- ✅ Asegúrate de que Docker está ejecutándose

### El scheduler no encuentra trenes
- ✅ Revisa la consola: `docker-compose logs -f renfe_scheduler`
- ✅ Comprueba que Renfe no ha cambiado su estructura HTML
- ✅ Aumenta el timeout en `scraper.py` si va lento

### Los trenes devuelven la fecha de hoy
- ✅ El bot busca trenes según la fecha que proporciones en formato DD/MM/AAAA
- ✅ Si no aparecen resultados, es posible que Renfe no haya trenes ese día
- ✅ Prueba con una fecha de fin de semana

### Errores de conexión de Telegram
- ✅ Verifica tu conectividad a Internet
- ✅ Revisa que el token sea válido (@BotFather)
- ✅ Asegúrate de que no hay firewall bloqueando salidas HTTPS

### `scripts/release.sh`/`.ps1` falla con "blob unknown to registry"
- ✅ Es un problema conocido del **containerd image store** de Docker
  Desktop (activado por defecto en versiones recientes): si el build y el
  push van en comandos separados, el content-store local no expone todos
  los blobs al hacer `docker push` después, y GitLab responde `blob unknown
  to registry`. Los scripts de release ya usan `docker buildx build --push`
  (build+push en un único paso) para evitarlo — si ves este error usando
  otro flujo manual, cambia a `docker buildx build --push` en vez de
  `docker build` + `docker push` por separado.

### `docker compose pull` falla con "no matching manifest for linux/amd64"
- ✅ La imagen se construyó para otra arquitectura (p. ej. build hecho en un
  Mac Apple Silicon, arm64) y el servidor es amd64. Los scripts de release
  construyen por defecto para `linux/amd64` (variable `PLATFORM`,
  configurable si tu servidor es arm64 o quieres publicar ambas
  arquitecturas) — vuelve a publicar el release con la versión actualizada
  de `scripts/release.sh`/`.ps1`.

## 🚀 Desplegar en producción (GitLab Container Registry)

En producción **no se hace build en el servidor**. La imagen se construye y
publica (build + tag + push) desde tu máquina de desarrollo hacia el GitLab
Container Registry, y el servidor solo descarga (`pull`) la imagen ya
verificada y la arranca. Esto da versionado real de imágenes y rollback
trivial: si un deploy rompe algo, basta con volver a apuntar al tag anterior,
sin reconstruir nada.

`docker-compose.yml` (con `build: .`) se mantiene tal cual para desarrollo
local — sigue siendo el onboarding más simple para quien clona el repo. Para
producción se usan dos ficheros nuevos: `compose.prod.yml` (usa `image:` en
vez de `build:`) y `.env.prod` (variables de producción, nunca se commitea).

### Configuración inicial (una sola vez)

**1. Crea un proyecto en GitLab** (si no tienes cuenta, créala gratis en
[gitlab.com](https://gitlab.com)):

- "New project" → "Create blank project". No hace falta subir código: este
  proyecto solo se usa para alojar el Container Registry.
- El path de la imagen es siempre `registry.gitlab.com/<namespace>/<proyecto>`,
  igual que la URL del proyecto. Puedes verlo tal cual en **Deploy →
  Container Registry** (GitLab te muestra ahí el path exacto y ejemplos de
  `docker login`/`docker push`).
- El Container Registry viene habilitado por defecto en gitlab.com. Si no
  aparece la sección "Container Registry" en el menú, revísalo en
  **Settings → General → Visibility, project features, permissions**.

> Para TrainPicker, el proyecto ya está creado en
> https://gitlab.com/trainpicker-group/trainpicker-project, así que la
> imagen es `registry.gitlab.com/trainpicker-group/trainpicker-project`
> (usado como ejemplo en el resto de esta sección). Verifica en **Deploy →
> Container Registry** de ese proyecto que el registry esté habilitado antes
> de seguir con el paso 2.

**2. Crea dos Deploy Tokens** (Settings → Repository → Deploy tokens), uno
para publicar y otro para desplegar, con el mínimo permiso necesario cada
uno:

- **Push (máquina de desarrollo):** scope `write_registry` (incluye lectura).
  Úsalo para autenticar el `docker push` de `scripts/release.sh`/`.ps1`:
  ```bash
  docker login registry.gitlab.com -u <usuario-del-token> -p <token>
  ```
- **Pull (servidor de producción):** scope `read_registry` únicamente —
  así el servidor nunca tiene permiso de escritura sobre el registry.
  Autentica el servidor una vez con ese token de la misma forma
  (`docker login registry.gitlab.com -u ... -p ...`); las credenciales
  quedan guardadas en `~/.docker/config.json` y no hace falta repetir el
  login en cada deploy.

Guarda ambos tokens en un gestor de contraseñas: GitLab solo los muestra una
vez al crearlos.

**3. Prepara los ficheros de producción en el servidor** (una sola vez, tras
clonar el repo):

```bash
cp .env.prod.example .env.prod
```

Edita `.env.prod` y rellena `GITLAB_REGISTRY_IMAGE` con
`registry.gitlab.com/trainpicker-group/trainpicker-project` y el resto de
variables de la aplicación (token de Telegram, intervalos) igual que harías
en `.env`. Deja `IMAGE_TAG` vacío por ahora — se rellena en cada release.

### Publicar un release (desde tu máquina, no en el servidor)

```bash
git status --short   # confirma que no queda nada sin commitear
export GITLAB_REGISTRY_IMAGE=registry.gitlab.com/trainpicker-group/trainpicker-project
./scripts/release.sh
```

En Windows/PowerShell:

```powershell
$env:GITLAB_REGISTRY_IMAGE = "registry.gitlab.com/trainpicker-group/trainpicker-project"
./scripts/release.ps1
```

El script aborta si hay cambios sin commitear, construye la imagen, la
taggea con un tag versionado nuevo (`AAAA.MM.DD.N`, p. ej. `2026.01.15.1` —
nunca reutiliza uno ya publicado) y la publica en el registry. Al terminar
imprime el tag generado.

### Desplegar en el servidor

```bash
ssh usuario@tu-servidor
cd trainpicker
# Edita .env.prod y pon IMAGE_TAG=<tag que imprimió el script de release>
docker compose --env-file .env.prod -f compose.prod.yml pull
docker compose --env-file .env.prod -f compose.prod.yml up -d
```

Verifica el despliegue:

```bash
docker compose --env-file .env.prod -f compose.prod.yml ps
docker compose --env-file .env.prod -f compose.prod.yml logs -f
```

### Rollback

Si algo falla, no hace falta reconstruir nada: vuelve a poner en `.env.prod`
el `IMAGE_TAG` del release anterior (sigue disponible en el registry) y
repite el `pull` + `up -d`:

```bash
# .env.prod: IMAGE_TAG=<tag anterior que funcionaba>
docker compose --env-file .env.prod -f compose.prod.yml pull
docker compose --env-file .env.prod -f compose.prod.yml up -d
```

(Opcional) Usa `systemctl` o `supervisor` para reiniciar automáticamente los
contenedores si el host se reinicia.

## 📄 Licencia

Este proyecto se distribuye bajo licencia MIT. Úsalo libremente en proyectos personales.

## 🙋 Contribuciones

¿Encontraste un bug o tienes una idea? Abre un issue o envía un pull request.

---

**Hecho con ❤️ para ferrocarrileros y viajeros frecuentes en Renfe.**