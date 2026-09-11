# Issue #25 — Falso positivo de disponibilidad (San Bernardo → Puerto de Santa María, 13/09/2026)

Notas de la investigación del bug reportado: la web de Renfe muestra
*"Tren completo, sin plazas disponibles en este horario"* para el tren de las
**18:12 (llegada 19:26)** del **13/09/2026** en el trayecto **San Bernardo →
Puerto de Santa María**, y TrainPicker lo marca como disponible.

## 0. Limitación de esta investigación (superada — ver §6)

La primera pasada de esta investigación **no pudo consultar Renfe en vivo**:
la sesión desde la que se hizo tenía la salida a `www.renfe.com` bloqueada por
política de red (el proxy de egress respondía `403` al `CONNECT`), así que no
fue posible capturar la respuesta real de `getTrainsList.dwr` ni ejecutar
Playwright contra la web. Se trabajó con payloads DWR **reconstruidos** (con
los nombres de campo reales que ya usa el scraper) y tests de regresión sobre
esos payloads.

Una sesión posterior (11/09/2026, ver §6) sí tuvo acceso y verificó contra
Renfe real: las causas 2.2-2.5 se confirmaron correctas tal cual, y 2.1 se
confirmó real pero con un matiz — el mecanismo de identidad por `cdgoTren` que
la primera pasada diseñó y probó **no funcionaba en producción** porque ese
campo no vive donde el fix lo leía. Corregido en la misma sesión de
verificación; detalle en §6.2.

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
de salida. De ahí salen los fallos que siguen.

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
  - Identidad del tren = `(codigosTrenTramo, salida, llegada, origen, destino)`.
    El OR de disponibilidad se mantiene, pero solo entre bloques que son
    literalmente el mismo tren. `codigosTrenTramo` sale de `cdgoTren`, que
    **no** es un campo del itinerario sino de cada tramo dentro de
    `trayectos: [...]` (descubierto en la verificación en vivo, ver §6):
    leerlo como campo de primer nivel (como hacía la primera versión de este
    fix) da siempre `None`, y la identidad se queda reducida a
    `(salida, llegada, origen, destino)` sin avisar. `_codigos_tren_tramo`
    busca `cdgoTren:"..."` dentro del rango completo del itinerario (tramos
    incluidos) para no depender de descender explícitamente en la estructura.
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
  - Si la respuesta trae itinerarios pero ninguno es de la fecha pedida, se
    loguea un warning con las fechas que sí venían (ver 5.3).
- **`main.py`**: el `callback_data` del botón lleva salida **y** llegada, la
  cabecera del listado usa la ruta mayoritaria en vez de la del primer tren, y
  los botones antiguos (sin llegada) siguen funcionando.
- **`scheduler.py`**: `_alert_matches_train` compara salida y llegada; las
  alertas antiguas con `arrival_time` vacío siguen casando solo por salida.

## 5. Pasada de verificación

Además de los 26 tests, se hicieron tres comprobaciones sobre el parser nuevo:

### 5.1 No hay regresión en el caso sano

Con una respuesta de 8 itinerarios sin duplicados de hora ni anidamiento (3 de
ellos completos), el parser nuevo devuelve **exactamente lo mismo** que el
anterior: mismos trenes, mismas llegadas, misma disponibilidad. Los cambios
solo alteran el resultado en los escenarios que estaban mal.

### 5.2 Termina y no rompe ante respuestas mutiladas

El recorrido nuevo lleva índices a mano (comillas, corchetes, llaves), así que
se probó con 4.000 payloads mutados al azar (truncados, con llaves/comillas
sueltas insertadas y caracteres borrados): **0 excepciones y 0,8 ms en el peor
caso**. Ni cuelgues por bucle infinito ni respuestas rotas que tumben el bot.

### 5.3 Riesgo asumido y blindado: `fecha` debe ser campo del itinerario

El parser nuevo lee `fecha` solo del objeto del itinerario, no de cualquier
sitio del bloque. Si Renfe moviese esa fecha a un objeto interno, el resultado
sería una lista vacía y el bot diría "no se han encontrado trenes" para
**todas** las búsquedas, en silencio.

Que hoy es un campo del propio itinerario se deduce de que el troceado anterior
(`split` por un nombre de campo + `re.search`) funcionaba en producción, lo que
exige que `fecha` aparezca después del marcador y antes de cualquier anidado —
consistente con el orden alfabético con el que DWR serializa el bean. Aun así,
por si acaso, ahora se loguea un warning con las fechas que sí traía la
respuesta cuando hay itinerarios pero ninguno coincide.

### 5.4 Flujo completo, de punta a punta

La prueba decisiva: el caso reportado (directo 18:12-19:26 completo conviviendo
con un itinerario con enlace que sale a la misma hora y sí tiene plaza) pasado
por el código real de `scraper.py` → `main.py` → SQLite → `scheduler.py`.

Con el código anterior:

```
1) Listado que ve el usuario:
      🕒 18:12 - 20:05 | ✅ DISPONIBLE
     botones de alerta: NINGUNO
>>> el bot NO ofrece alerta para el tren completo de las 18:12
```

El tren de las 18:12-19:26 ni siquiera aparece: lo ha sustituido el otro
itinerario, pintado como disponible. Es exactamente el síntoma reportado.

Con el fix:

```
1) Listado que ve el usuario:
      🕒 18:12 - 19:26 | ❌ COMPLETO
      🕒 18:12 - 20:05 | ✅ DISPONIBLE
     botones de alerta: ['alerta_18:12_19:26']
2) Pulsa 'alerta_18:12_19:26' -> alerta para 18:12-19:26
3) Ciclo del scheduler -> notificaciones enviadas: 0
```

Y la contraprueba, para asegurar que no se ha roto la función del bot: con la
alerta guardada en 18:12-19:26,

| Estado de Renfe | Notifica |
|---|---|
| Solo el enlace de las 18:12 tiene plaza (el directo sigue completo) | no (antes: sí) |
| Se libera el directo 18:12-19:26 | **sí** |
| Todo completo | no |

## 6. Verificación en vivo (11/09/2026, contra Renfe real)

Hecha con Playwright vía el propio `scraper.refresh_session` (espiando
`parsear_dwr_renfe` para volcar el texto crudo) y comparada a mano con
`venta.renfe.com`. Fecha real de la verificación: **11/09/2026**, dos días
antes del 13/09/2026 que reporta la issue.

### 6.1 El caso literal de la issue ya no se reproduce tal cual

El 18:12-19:26 de San Bernardo → Puerto de Santa María del 13/09/2026 **ya no
sale como "solo completo, sin alternativa"**: la web muestra ahora **dos**
tarjetas para ese mismo horario nominal —

- **Tren 13083**: reservable, 12,70 €, con las condiciones de tarifa reales
  (confirmado haciendo clic en la tarjeta y viendo la ficha de compra: "Adulto
  ida 12,7€ / Cambios 0% primer cambio, siguientes 10% / Anulaciones 15%").
- **Tren 35083**: `railway_alert Tren Completo`.

Esto es esperable: la disponibilidad de Renfe es inventario en tiempo real y
la issue se reportó bastante antes del vuelo. El caso instructivo para la
verificación no es "¿el bot dice False?" (ahora sí habría un asiento real
comprable en ese horario), sino **si el bot pierde el hecho de que 35083 sigue
completo al fundirlo con 13083**, que es justo el mecanismo que describe 2.1.

### 6.2 2.1 confirmada con datos reales — y un hallazgo nuevo: `cdgoTren` no existe donde el fix lo buscaba

`scripts/diagnosticar_dwr.py respuesta.dwr 13/09/2026 --salida 18:12` mostró,
antes de esta sesión, **dos itinerarios reales** en 18:12-19:26 (`tren=None`
en ambos):

```
18:12 - 19:26  tren=None  SAN BERNARDO -> PUERTO DE SANTA MARÍA
    disponible=True
    señales: completo=false, tarifasDisponibles=[, razonNoDisponible="8", soloPlazaH=false

18:12 - 19:26  tren=None  SAN BERNARDO -> PUERTO DE SANTA MARÍA
    disponible=False  motivo=sin_tarifas+razon_3
    señales: completo=false, tarifasDisponibles=null, razonNoDisponible="3", soloPlazaH=false
```

`parsear_dwr_renfe` los fundía en una sola entrada `disponible=True` — el
mecanismo de 2.1 reproducido letra por letra con datos reales, solo que hoy la
fusión "acierta" (13083 sí tiene hueco) en vez de ocultar un completo sin
alternativa. El `tren=None` en los dos es la pista: **`cdgoTren` no es un
campo del itinerario**. Inspeccionando el bloque crudo completo (no solo los
campos de primer nivel que lee `_extract_object_fields`), aparece anidado
dentro de `trayectos`:

```
...tarifasDisponibles:[{...,tarifaTramoCombViewBean:[{...,cdgoTren:"13083",...}],...}],
...trayectos:[{...,cdgoTren:"13083",...,razonNoDisponible:null,...}],...
```

para el itinerario disponible, y `trayectos:[{...,cdgoTren:"35083",...,
razonNoDisponible:"3",...}]` para el completo. **Son dos trenes reales y
distintos** (13083 y 35083) que casualmente comparten horario nominal — no
"el mismo tren en dos filas de tarifa". Pero como `_extract_object_fields`
descarta a propósito el contenido de arrays/objetos anidados (es la defensa de
2.2 contra los tramos fantasma: solo guarda el primer carácter `[`/`{` y
salta el resto), `_text_field(fields, "cdgoTren")` devolvía `None` **siempre**,
en producción, para cualquier itinerario real. La identidad quedaba reducida a
`(salida, llegada, origen, destino)` — exactamente la misma agrupación que
antes del fix — y el `cdgoTren` de la identidad documentada en el código y
probado en `tests/test_parseo_dwr.py` (que lo serializaba en el nivel
superior del itinerario, no anidado) nunca se ejercitó contra una forma real
de la respuesta.

**Corregido en esta sesión** (`scraper.py`): `_extract_object_fields` ahora
también devuelve dónde cierra el objeto del itinerario, y
`_codigos_tren_tramo` busca `cdgoTren:"..."` en todo ese rango (tramos
incluidos, para que un enlace multi-tramo componga su identidad con todos los
trenes que lo forman). `codigo_tren` deja de leer el campo de primer nivel
inexistente y pasa a usar esto. Verificado contra la misma respuesta real:

```
18:12 - 19:26  tren=13083  ...  disponible=True
18:12 - 19:26  tren=35083  ...  disponible=False  motivo=sin_tarifas+razon_3
```

Ahora se listan por separado, cada uno con su disponibilidad real. Mismo
patrón confirmado en 20:53 (13035 con hueco + 35035 completo) y 22:13 (13073
con hueco + 35073 completo) de la misma respuesta.

`tests/test_parseo_dwr.py` se actualizó en dos frentes: el fixture
`itinerario()` ahora serializa `cdgoTren` anidado en `trayectos` (como hace
Renfe de verdad, no en el nivel superior) y se añadió
`test_dos_trenes_reales_en_la_misma_franja_no_se_funden`, que reproduce
literalmente el par 13083/35083 visto en vivo como test de regresión con
datos auténticos (anonimizado: son los códigos de tren reales, sin datos de
usuario).

### 6.3 Resto del listado

El resto de horarios del 13/09/2026 San Bernardo → Puerto de Santa María
cuadra con la web tren a tren: `disponible=True` con precio y ambas plazas
cuando la web ofrece tarifa; `disponible=False, motivo=solo_plaza_h` cuando la
web marca "Solo plaza H disponible" (la restricción de negocio deliberada de
TrainPicker, sin tocar). No se encontraron itinerarios con
`motivo=formato_desconocido` (fail-closed de 2.3) ni trenes fantasma (2.2) en
ninguna de las capturas.

### 6.4 Backlog: vinculado al Project

La issue #25 se añadió al GitHub Project 15 (`Tipo=Bug`, `Priority=P0`,
`Status=In review`) — ver protocolo en `CLAUDE.md`.

## 7. Hallazgos anotados y NO corregidos aquí

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
