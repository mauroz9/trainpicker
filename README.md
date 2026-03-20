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
├── docker-compose.yml      # Configuración de Docker Compose
├── Dockerfile              # Imagen Docker personalizada
├── requirements.txt        # Dependencias Python
├── .env.example            # Plantilla de variables de entorno
├── .gitignore              # Archivos ignorados en Git
└── README.md               # Este archivo
```

## 🔧 Configuración avanzada

### Cambiar intervalo de revisión de alertas

En `scheduler.py`, línea 38, cambia `seconds=5` por el intervalo que prefieras:
```python
# Para revisar cada 5 segundos en producción:
scheduler.add_job(check_alerts, 'interval', seconds=5)

# Para cada 30 segundos (depuración):
scheduler.add_job(check_alerts, 'interval', seconds=30)
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

## 🚀 Desplegar en producción

Para un servidor Linux/VPS (ej. AWS, Linode, DigitalOcean):

1. Conecta por SSH y clona el repositorio
2. Copia `.env.example` a `.env` y configura el token
3. Ejecuta: `docker-compose up -d`
4. (Opcional) Usa `systemctl` o `supervisor` para reiniciar automáticamente si cae

## 📄 Licencia

Este proyecto se distribuye bajo licencia MIT. Úsalo libremente en proyectos personales.

## 🙋 Contribuciones

¿Encontraste un bug o tienes una idea? Abre un issue o envía un pull request.

---

**Hecho con ❤️ para ferrocarrileros y viajeros frecuentes en Renfe.**