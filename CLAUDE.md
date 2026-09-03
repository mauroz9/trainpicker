# TrainPicker — contexto para Claude

Bot de Telegram (Python) que vigila disponibilidad de billetes en Renfe y avisa cuando
se libera plaza en un tren completo.

- `main.py` — bot conversacional (python-telegram-bot)
- `scheduler.py` — comprobación periódica de alertas (APScheduler)
- `scraper.py` — Playwright + réplica de sesión/token de Renfe vía `httpx`
- `database.py` — SQLite compartida entre bot y scheduler vía volumen Docker

Contexto que no es un bug:
- La caché de sesión/token de Renfe (`session_cache` en `database.py`) se reutiliza con
  `httpx` en `scraper.py` para pegar directo a la API sin abrir navegador. Es
  intencional: es la ruta rápida real.
- El usuario asume conscientemente el riesgo de rate-limit/baneo por parte de Renfe. No
  añadir límites de frecuencia "por seguridad" sin que se pida explícitamente.

## Backlog técnico — vive en GitHub Projects, no en este archivo

El backlog técnico se gestiona en el **GitHub Project "Project TrainPicker"**
(https://github.com/users/mauroz9/projects/15), owner `mauroz9`, repo
`mauroz9/trainpicker`. Cada tarea es un Issue del repo añadido al Project, con tres
campos custom/single-select: `Tipo`, `Priority`, `Status`.

Antes de tocar el backlog, refresca los ids (pueden cambiar si se recrean campos):

```bash
gh project field-list 15 --owner mauroz9 --format json
gh project item-list 15 --owner mauroz9 --format json
```

IDs vigentes a 2026-09-03 (verificar con los comandos de arriba si algo falla):
- `PROJECT_ID`: `PVT_kwHOCvM9HM4BiVdK`
- Campo `Tipo` (`PVTSSF_lAHOCvM9HM4BiVdKzhhN-MY`): Seguridad, Rendimiento, Fiabilidad,
  Bug, Calidad, Feature, Documentación, Operación
- Campo `Priority` (`PVTSSF_lAHOCvM9HM4BiVdKzhhN41g`): P0, P1, P2, P3
- Campo `Status` (`PVTSSF_lAHOCvM9HM4BiVdKzhhN4ug`): Backlog, Ready, In progress,
  In review, Done

### Protocolo para tareas nuevas

1. Clasificar en `Tipo`: Seguridad, Rendimiento, Fiabilidad, Bug, Calidad, Feature,
   Documentación, Operación.
2. Asignar `Priority`: P0 (crítico: pérdida de datos, secretos expuestos, el bot deja
   de notificar), P1 (alto: degrada fiabilidad/experiencia claramente), P2 (medio,
   mejora relevante no urgente), P3 (bajo, pulido).
3. Crear un Issue en `mauroz9/trainpicker` (`gh issue create`) con:
   - Título: el nombre corto de la tarea.
   - Body: `**Tipo:** ... | **Prioridad:** ...` + sección `**Descripción:**` (qué está
     mal) + sección `**Cómo resolverlo:**` (cómo, no solo qué).
4. Añadirlo al Project (`gh project item-add 15 --owner mauroz9 --url <issue_url>`) y
   fijar los tres campos vía GraphQL (`updateProjectV2ItemFieldValue`), `Status` inicial
   = `Backlog`.
5. Si se implementa en el mismo turno: mover `Status` a `Done`, cerrar el issue
   (`gh issue close`) y comentar qué archivos se tocaron. Si se empieza sin acabar:
   `Status` = `In progress`.
6. Nunca borrar issues del backlog: si se descarta, cerrar como "not planned" con el
   motivo en un comentario, no eliminar.
7. Antes de dar por buena una P0/P1 abierta, verificar en el código actual que el
   problema sigue existiendo (no asumir que el Project está al día).

Para consultar el estado actual del backlog:

```bash
gh project item-list 15 --owner mauroz9 --format json
```
