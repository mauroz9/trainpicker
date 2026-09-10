# Issue #25 — Falso positivo de disponibilidad (San Bernardo → Puerto de Santa María, 13/09/2026)

Notas de la investigación del bug reportado: la web de Renfe muestra
*"Tren completo, sin plazas disponibles en este horario"* para el tren de las
**18:12 (llegada 19:26)** del **13/09/2026** en el trayecto **San Bernardo →
Puerto de Santa María**, y TrainPicker lo marca como disponible.

## 0. Limitación de esta investigación

**No se pudo consultar Renfe en vivo.** La sesión desde la que se hizo este
trabajo tiene la salida a `www.renfe.com` bloqueada por política de red (el
proxy de egress responde `403` al `CONNECT`), así que no fue posible capturar
la respuesta real de `getTrainsList.dwr` para esa fecha ni ejecutar Playwright
contra la web.

Qué se hizo en su lugar:

- Análisis estático de `parsear_dwr_renfe` y del flujo completo de la alerta.
- Reproducción del fallo con payloads DWR **reconstruidos** con los nombres de
  campo reales que ya usa el scraper (los capturó el autor en `8c6b853` y se
  ampliaron en el PR #16, donde se volcó además el `listaTrenes.js` real de
  Renfe con su lógica de decisión).
- Tests de regresión que fijan el comportamiento correcto (`tests/`).
- Un diagnosticador (`scripts/diagnosticar_dwr.py`) para que la verificación
  contra la respuesta real sea un solo comando desde una máquina con acceso.

Por tanto: **las causas están demostradas sobre el código** (cada una reproduce
el síntoma exacto), pero *cuál* de ellas es la que dispara el caso concreto de
las 18:12 solo puede confirmarlo el volcado real. La verificación pendiente
está al final.

## 1. Qué mira el bot y de dónde salen los datos

El scraper no lee el HTML: replica la sesión y pega directamente contra
`.../trainEnlacesManager.getTrainsList.dwr`, la misma llamada que hace el
frontend. La respuesta es un volcado DWR con los itinerarios serializados como
literales de objeto JavaScript. `parsear_dwr_renfe` la traduce a la lista de
dicts que consumen `main.py` (listado + botón de alerta) y `scheduler.py`
(notificación).

El detalle que lo explica casi todo está en el nombre del endpoint:
`trainEnlaces`. La respuesta **no es una lista de trenes**, es una lista de
**itinerarios**: directos, con enlace (transbordo) y con acercamiento
(`acercamientoViajeOrigen` / `acercamientoViajeDestino`, los campos con los que
el propio parser trocea el texto). Varios itinerarios distintos pueden salir a
la misma hora, y un itinerario con enlace contiene dentro sus tramos, que
repiten la misma forma.

El parser trataba esa lista como si fuera un tren por bloque, indexado por hora
de salida. De ahí salen los tres fallos.

## 2. Causas encontradas

### 2.1 Deduplicado por hora de salida con OR de disponibilidad (causa principal)

```python
existente = trenes_unicos.get(salida)          # ← la clave es SOLO la hora de salida
if existente is None or (not existente["disponible"] and tren_data["disponible"]):
    trenes_unicos[salida] = tren_data          # ← reemplaza el tren entero
elif tren_data["disponible"]:
    existente["disponible"] = True             # ← OR de disponibilidad
```

Dos itinerarios distintos que salen a las 18:12 colapsan en una sola entrada y,
si cualquiera de ellos tiene plaza, la entrada queda como disponible. Peor: al
reemplazar el dict completo, la llegada, el código de tren y las estaciones que
se muestran pasan a ser las del *otro* itinerario.

El OR se añadió a propósito ("si un tren sale 2 veces y en una tarifa sí hay
hueco, prima el hueco"), y para ese caso es correcto — pero la clave elegida no
distingue "el mismo tren dos veces" de "dos trenes a la misma hora".

Hay evidencia de que esta ruta tiene duplicados de hora de salida: en el
análisis del PR #16, sobre este mismo trayecto, **34 bloques colapsaron a 26
trenes únicos**.

### 2.2 Los "bloques" no son objetos: los anidados se cuelan

```python
bloques = texto_dwr.split('acercamientoViajeDestino:')
...
completo_m = re.search(r'completo:\s*(true|false)', bloque)
```

Trocear por un nombre de campo y luego buscar cada campo con `re.search` sobre
todo el bloque ignora el anidamiento. Los tramos de un enlace llevan el mismo
marcador, así que:

- el itinerario real se corta donde empieza su tramo interno, y
- el tramo interno entra en el listado **como si fuera un tren más**, con su
  propia hora de salida y su propia disponibilidad.

En la reproducción, un tren completo con un tramo interno con plaza desaparece
del listado y en su lugar aparece un tren fantasma 18:12-18:47 disponible, sin
estaciones.

### 2.3 `razonNoDisponible: ""` se trataba como bloqueante (fallo simétrico)

La lógica real de Renfe, volcada en el PR #16 desde `listaTrenes.js`, es:

```js
} else if ((razon != "" && razon != "8") || tarifasDisponibles == null) {
```

es decir, la cadena vacía significa "sin incidencia". El código comprobaba
`razon_m.group(1) not in ('null', '"8"')`, así que `""` bloqueaba la venta. El
docstring ya decía "presente, **no vacío** y distinto de 8": era la
implementación la que no seguía a su propia documentación. Si Renfe sirve `""`
en vez de `null` en esta ruta, **todos** los trenes salen completos (falso
negativo).

### 2.4 `_decode_escaped_text` corrompía los nombres de estación ya decodificados

`codecs.decode(value, "unicode_escape")` no solo resuelve los escapes: también
reinterpreta el texto byte a byte. Si la respuesta ya venía decodificada (httpx
decodifica el cuerpo según el charset), el resultado era mojibake:

```
'EL PUERTO DE SANTA MARÍA'  ->  'EL PUERTO DE SANTA MARÃ\x8dA'
```

No es cosmético: `main.py` guarda ese nombre como origen/destino de la alerta y
`scheduler.py` lo reescribe en el autocompletado de Renfe al recapturar la
sesión con Playwright.

### 2.5 La notificación casaba solo por hora de salida

```python
if user_data['train_time'] != tren_web.get('salida'):
    continue
```

Aunque el parser distinga los dos itinerarios de las 18:12, el aviso se
dispararía igual con el que no es. Y el aviso es destructivo: tras enviarlo,
`_notify_users_for_route` llama a `delete_alert`, así que un falso positivo no
solo molesta — **borra la vigilancia** del tren que el usuario quería.
`arrival_time` ya estaba guardado en la tabla `alerts`, solo faltaba usarlo.

## 3. Reproducción

`tests/test_parseo_dwr.py` recoge estos escenarios. Comparación directa del
mismo payload contra el código anterior y el corregido:

| Escenario (13/09/2026, salida 18:12) | Renfe | Antes | Después |
|---|---|---|---|
| Directo 18:12-19:26 `completo:true` (solo) | completo | ❌ completo | ❌ completo |
| Directo completo **+ enlace 18:12-20:05 con plaza** | completo | ✅ **disponible** (y el directo desaparece del listado) | ❌ completo, y el enlace se lista aparte |
| Directo completo **con un tramo interno con plaza** | completo | ✅ **disponible** como 18:12-18:47 fantasma | ❌ completo, sin tren fantasma |
| `razonNoDisponible:""` | comprable | ❌ completo | ✅ disponible |

Las dos filas centrales son el síntoma reportado: la web dice completo y el bot
dice disponible.

## 4. Qué se ha corregido

- **`scraper.py`**
  - `_iter_itinerary_fields`: recorre la respuesta llevando la cuenta del
    anidamiento y se queda con los `acercamientoViajeDestino:` de profundidad
    mínima (los itinerarios del listado), descartando los internos.
  - `_extract_object_fields`: lee los campos del propio objeto y salta entero
    cualquier objeto o array anidado, de modo que ningún campo de un tramo o de
    una tarifa se atribuye al itinerario.
  - Identidad del tren = `(cdgoTren, salida, llegada, origen, destino)`. El OR
    de disponibilidad se mantiene, pero solo entre bloques que son literalmente
    el mismo tren.
  - `evaluar_disponibilidad` extraída y alineada con `listaTrenes.js`: `""`,
    `null` y `"8"` no bloquean; cualquier otro código sí. Se conserva
    `soloPlazaH` como completo (restricción de negocio de TrainPicker) y el
    fail-closed ante formato desconocido (#5).
  - Campo nuevo `motivo` (`completo`, `sin_tarifas`, `razon_4`, `solo_plaza_h`,
    `formato_desconocido`, o su combinación) para no volver a diagnosticar a
    ciegas.
  - `_decode_escaped_text` sustituye solo los escapes `\uXXXX`/`\xXX`.
  - Los errores se capturan por itinerario: un bloque raro ya no vacía la lista
    entera.
- **`main.py`**: el `callback_data` del botón lleva salida **y** llegada, la
  cabecera del listado usa la ruta mayoritaria en vez de la del primer tren, y
  los botones antiguos (sin llegada) siguen funcionando.
- **`scheduler.py`**: `_alert_matches_train` compara salida y llegada; las
  alertas antiguas con `arrival_time` vacío siguen casando solo por salida.

## 5. Verificación pendiente (requiere acceso a Renfe)

1. Buscar San Bernardo → Puerto de Santa María para el 13/09/2026 en
   renfe.com, DevTools → Red → filtrar `getTrainsList.dwr` → "Copiar
   respuesta" → guardarla en `respuesta.dwr`.
2. Ejecutar:

   ```bash
   python3 scripts/diagnosticar_dwr.py respuesta.dwr 13/09/2026 --salida 18:12
   ```

3. Comprobar que el itinerario 18:12-19:26 sale con `disponible=False` y qué
   `motivo` da, y ver cuántos itinerarios más comparten la salida de las 18:12
   (que es lo que confirmaría 2.1 como causa concreta de este caso).

Si el itinerario 18:12-19:26 apareciese ahí con todas las señales en "libre"
(`completo=false`, `tarifasDisponibles=[`, `razonNoDisponible` vacío/nulo/`8`,
`soloPlazaH=false`) mientras la web lo pinta completo, entonces la causa sería
otra distinta: que Renfe decide el "Tren completo" con información que no está
en esta respuesta. En ese caso, el volcado del diagnosticador es exactamente lo
que hace falta para abrir el siguiente issue.

## 6. Hallazgos anotados y NO corregidos aquí

- **La ruta de la alerta no es la de la búsqueda.** `main.py` busca con lo que
  escribe el usuario ("San Bernardo") pero guarda la alerta con la descripción
  que devuelve Renfe ("Sevilla-San Bernardo"). Como `build_search_key` se
  construye con esos nombres, la sesión cacheada durante la búsqueda **nunca**
  se reutiliza para esa alerta: el scheduler la recaptura con Playwright usando
  el nombre de Renfe. Está emparentado con la issue #8 (claves de caché sin
  normalizar) y merece tratarse allí.
- **`.title()` sobre los nombres de estación** produce "El Puerto De Santa
  María" (con "De" en mayúscula). Cosmético, pero esos nombres se guardan en la
  BD y se reescriben en el autocompletado, así que cambiarlo mueve claves de
  caché y alertas existentes: fuera del alcance de este fix.
- **`tarifaMinima` numérica** (`13.2` sin comillas) ya se lee bien tras el
  cambio, pero `precio_desde` sigue sin usarse en ningún mensaje del bot.
