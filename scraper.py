import asyncio
import json
import logging
import os
import re
import tempfile
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx
from playwright.async_api import Browser, Playwright, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from database import build_search_key, delete_session_cache, get_session_cache, upsert_session_cache

logger = logging.getLogger(__name__)
ALLOWED_RESOURCE_TYPES = ["document", "script", "xhr", "fetch"]
AUTOCOMPLETE_TYPE_DELAY_MS = 60

# Espera normal para que la busqueda dispare la peticion DWR.
DWR_TIMEOUT_S = 30.0
# Espera adicional SOLO cuando Renfe nos ha metido en su sala de espera
# virtual (Queue-it). Ahi no se dispara ninguna peticion hasta que llega
# nuestro turno, y los turnos observados rondan los 1-3 minutos.
QUEUE_TIMEOUT_S = 420.0
QUEUE_HOST = "queue-it.net"
# Margen para que Renfe confirme la estacion elegida (rellena el hidden de
# forma asincrona tras el click, no en el propio manejador).
STATION_CONFIRM_TIMEOUT_MS = 5000

# Cookies de la ultima captura, persistidas en el volumen compartido para que
# el pase de la cola virtual (Queue-it da ~20 min) y la sesion de Renfe
# sobrevivan a un reinicio del contenedor: sin esto, cada reinicio empezaba
# haciendo cola otra vez en la primera ruta.
STORAGE_STATE_PATH = os.path.join("data", "renfe_storage_state.json")
# Tope duro al fichero persistido. `storage_state` de Playwright incluye el
# `localStorage`/`sessionStorage` de cada origen, que puede crecer sin control;
# como para replicar la sesion solo necesitamos las cookies, se guardan SOLO
# esas y se descarta el resto. El tope es una red de seguridad extra por si un
# dia las cookies se disparan: mejor rehacer cola que llenar el disco.
STORAGE_STATE_MAX_BYTES = 256 * 1024


# Callback opcional que se invoca (una vez) al detectar la cola virtual, para
# que quien haya pedido la busqueda pueda avisar al usuario de que toca esperar.
AvisoColaCallback = Callable[[Dict[str, Any]], Awaitable[None]]


class ScraperRenfeError(Exception):
    """Fallo capturando la sesion de Renfe (distinto de 'no hay trenes')."""


class RenfeEnColaError(ScraperRenfeError):
    """Se agoto la espera en la sala de espera virtual de Renfe."""


class EstacionNoConfirmadaError(ScraperRenfeError):
    """El autocompletado de Renfe no confirmo la estacion seleccionada."""


# Contador de fallos consecutivos de _capture_session_with_playwright. Permite a
# scheduler.py detectar roturas del scraper (p.ej. Renfe cambia su web) que de
# otro modo son indistinguibles de "no hay trenes disponibles" (issue #22).
_consecutive_capture_failures: int = 0


def get_consecutive_capture_failures() -> int:
    return _consecutive_capture_failures


def _sanitize_headers(headers: Dict[str, str]) -> Dict[str, str]:
    return {
        key: value.encode("ascii", "ignore").decode("ascii")
        for key, value in headers.items()
    }


_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})")
_IDENT_RE = re.compile(r"([A-Za-z_$][A-Za-z0-9_$]*)\s*:")
_BLOCK_MARKER = "acercamientoViajeDestino:"
# Señales que deciden si un tren es comprable. Si ninguna aparece en el
# itinerario, asumimos que Renfe cambio el formato del DWR (fail-closed).
_AVAILABILITY_FIELDS = ("completo", "tarifasDisponibles", "razonNoDisponible", "soloPlazaH")


def _decode_escaped_text(value: str) -> str:
    """Convierte los escapes `\\uXXXX` / `\\xXX` del DWR en caracteres reales.

    Antes se usaba `codecs.decode(value, "unicode_escape")`, que ademas de
    resolver los escapes reinterpreta el texto byte a byte: si la respuesta ya
    venia decodificada (httpx decodifica el cuerpo segun el charset), "EL
    PUERTO DE SANTA MARIA" con tilde salia convertido en mojibake
    ("MARÃ\\x8dA"). Esos nombres corruptos no son solo cosmeticos: `main.py`
    los guarda como origen/destino de la alerta y `scheduler.py` los reescribe
    en el autocompletado de Renfe al recapturar la sesion.

    Sustituyendo solo los escapes se cubre el caso en que Renfe escapa el
    texto y se deja intacto el que ya viene decodificado.
    """
    def _replace(match: "re.Match[str]") -> str:
        return chr(int(match.group(1) or match.group(2), 16))

    try:
        return _UNICODE_ESCAPE_RE.sub(_replace, value)
    except Exception:
        return value


def _skip_string(text: str, index: int) -> int:
    quote = text[index]
    index += 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            return index + 1
        index += 1
    return index


def _skip_nested(text: str, index: int) -> int:
    """Devuelve el indice justo despues del `[`/`{` balanceado que abre en `index`."""
    depth = 0
    while index < len(text):
        char = text[index]
        if char in "\"'":
            index = _skip_string(text, index)
            continue
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return index


def _extract_object_fields(text: str, start: int) -> Tuple[Dict[str, str], int]:
    """Extrae los campos del objeto que empieza en `start`, sin entrar en los anidados.

    `start` apunta al valor de `acercamientoViajeDestino`, es decir, al interior
    del objeto del itinerario. Se leen sus campos hasta el `}` que lo cierra;
    todo objeto o array anidado (tarifas, tramos de un enlace) se salta entero,
    de forma que nunca aporta un `completo:`/`horaSalida:`/`fecha:` que no le
    corresponde al itinerario. Los valores se devuelven crudos (`"3"`, `null`,
    `true`, `[`), tal cual aparecen en el DWR.

    Devuelve tambien el indice donde se encontro el `}` de cierre (o `len(text)`
    si no aparecio), para que el llamante pueda acotar una busqueda propia
    dentro del objeto completo, anidados incluidos (ver `_codigos_tren_tramo`).
    """
    fields: Dict[str, str] = {}
    index = start
    length = len(text)

    while index < length:
        char = text[index]

        if char in "\"'":
            index = _skip_string(text, index)
            continue
        if char in "[{":
            index = _skip_nested(text, index)
            continue
        if char in "]}":
            break

        match = _IDENT_RE.match(text, index)
        if not match:
            index += 1
            continue

        name = match.group(1)
        value_start = match.end()
        while value_start < length and text[value_start] in " \t\r\n":
            value_start += 1

        if value_start >= length:
            break

        if text[value_start] in "\"'":
            value_end = _skip_string(text, value_start)
            raw = text[value_start:value_end]
        elif text[value_start] in "[{":
            raw = text[value_start]
            value_end = _skip_nested(text, value_start)
        else:
            value_end = value_start
            while value_end < length and text[value_end] not in ",}])":
                value_end += 1
            raw = text[value_start:value_end].strip()

        fields.setdefault(name, raw)
        index = value_end

    return fields, index


_LEG_TREN_RE = re.compile(r'cdgoTren:"([^"]*)"')


def _codigos_tren_tramo(texto_dwr: str, start: int, end: int) -> str:
    """Codigo(s) de tren real(es) del itinerario que ocupa `[start, end)`.

    `cdgoTren` no es un campo propio del itinerario (que `_extract_object_fields`
    ignoraria, al vivir anidado en `trayectos`), sino de cada tramo dentro de
    `trayectos: [...]`; un itinerario directo tiene un tramo (un cdgoTren), un
    enlace varios. Es el unico identificador real del tren fisico que trae el
    DWR: dos itinerarios pueden compartir salida/llegada/origen/destino siendo
    trenes distintos (p.ej. 13083 con plaza y 35083 "Tren Completo" saliendo
    ambos de San Bernardo a las 18:12 con llegada 19:26, visto en vivo el
    11/09/2026 para el 13/09/2026), y sin este codigo no hay forma de
    diferenciarlos: se fundirian por error via el OR de disponibilidad
    (issue #25). Se buscan dentro de todo el rango del itinerario (tramos
    incluidos) porque tambien aparece redundado en `tarifasDisponibles`.
    """
    return "+".join(sorted(set(_LEG_TREN_RE.findall(texto_dwr, start, end))))


_TARIFA_SOLO_PLAZAS_H_RE = re.compile(r'soloPlazasH:(true|false)')


def _tarifa_solo_plaza_reservada(texto_dwr: str, start: int, end: int) -> bool:
    """True si todas las filas de tarifa ofertadas son de plaza reservada.

    `tarifasDisponibles[].soloPlazasH` (anidado, plural) es la señal real de
    si la tarifa ofertada esta restringida a un tipo de plaza reservada (H o
    bicicleta): a pesar del nombre, en la practica no distingue el tipo, solo
    si la unica plaza que queda es de un cupo reservado. Es distinta -y mas
    fiable- que `soloPlazaH` (nivel superior, singular), que puede venir en
    `false` aunque la unica tarifa ofertada sea de plaza bicicleta (verificado
    en vivo: tren 13083, San Bernardo -> Puerto de Santa Maria, 12 de 794
    itinerarios reales muestreados en 3 rutas y 21 fechas no tenian ninguna
    plaza normal a pesar de `soloPlazaH=false`). En los 794 itinerarios
    muestreados nunca hay filas de tarifa mezcladas (unas restringidas y
    otras no): o todas lo estan o ninguna, pero por si acaso se exige que
    *todas* las filas ofertadas lo esten -si hay alguna sin restriccion, hay
    un asiento normal que reservar y no debe marcarse completo-. Sin ninguna
    fila de tarifa (tarifasDisponibles: null) devuelve False; ese caso ya lo
    cubre el motivo `sin_tarifas`.
    """
    valores = _TARIFA_SOLO_PLAZAS_H_RE.findall(texto_dwr, start, end)
    return bool(valores) and all(v == "true" for v in valores)


def _iter_itinerary_fields(texto_dwr: str) -> List[Dict[str, str]]:
    """Devuelve los campos de cada itinerario de la respuesta DWR.

    Localiza cada `acercamientoViajeDestino:` anotando a que profundidad de
    anidamiento aparece y se queda solo con los de profundidad minima: esos son
    los itinerarios que Renfe lista, mientras que los mas profundos son objetos
    interiores (p.ej. los tramos de un enlace, que repiten la misma forma). El
    troceado anterior con `split(marcador)` no distinguia unos de otros, asi que
    un tramo con plaza libre entraba en el listado como si fuera un tren mas.
    """
    occurrences: List[Tuple[int, int]] = []
    depth = 0
    index = 0
    length = len(texto_dwr)
    marker_length = len(_BLOCK_MARKER)

    while index < length:
        char = texto_dwr[index]

        if char in "\"'":
            index = _skip_string(texto_dwr, index)
            continue
        if char in "[{":
            depth += 1
            index += 1
            continue
        if char in "]}":
            depth -= 1
            index += 1
            continue
        if char == "a" and texto_dwr.startswith(_BLOCK_MARKER, index):
            occurrences.append((depth, index + marker_length))
            index += marker_length
            continue

        index += 1

    if not occurrences:
        return []

    top_depth = min(depth for depth, _ in occurrences)
    resultado = []
    for depth, start in occurrences:
        if depth != top_depth:
            continue
        fields, end = _extract_object_fields(texto_dwr, start)
        fields["_codigosTrenTramo"] = _codigos_tren_tramo(texto_dwr, start, end)
        fields["_tarifaSoloPlazaReservada"] = _tarifa_solo_plaza_reservada(texto_dwr, start, end)
        resultado.append(fields)
    return resultado


def _text_field(fields: Dict[str, str], name: str) -> Optional[str]:
    raw = fields.get(name)
    if raw is None or raw == "null":
        return None
    if len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0]:
        return _decode_escaped_text(raw[1:-1])
    return raw


def _bool_field(fields: Dict[str, str], name: str) -> Optional[bool]:
    raw = fields.get(name)
    if raw not in ("true", "false"):
        return None
    return raw == "true"


def evaluar_disponibilidad(fields: Dict[str, str]) -> Tuple[bool, Optional[str]]:
    """Decide si un itinerario es comprable y, si no lo es, por que.

    Replica la logica real del frontend de Renfe (`listaTrenes.js`, la funcion
    que elige entre `trenTemplateCompleto` / `trenTemplateBloqueado` /
    `trenTemplateNoCircula` / `trenTemplateNoVenta`) mas una restriccion de
    negocio propia de TrainPicker. Un tren NO esta disponible si:
      - `completo == true`.
      - `razonNoDisponible` esta presente, NO es vacio y es distinto de `"8"`.
        Renfe usa `""` para "sin incidencia" y `"8"` solo para avisos
        informativos (p.ej. limitaciones de velocidad de Adif) que no bloquean
        la venta; el resto de codigos observados -"3" completo, "4" trayecto
        bloqueado, "5"/"6"/"7" no circula- si la bloquean, igual que cualquier
        codigo nuevo no catalogado (el `else` final de esa misma funcion).
      - `tarifasDisponibles == null` (sin tarifas no hay nada que comprar).
      - `soloPlazaH == true` (nivel superior) o `tarifasDisponibles[].soloPlazasH
        == true` en todas las filas de tarifa ofertadas (anidado, ver
        `_tarifa_solo_plaza_reservada`): en el frontend de Renfe esto NO
        bloquea la compra (solo cambia que plantilla/icono se pinta), pero
        significa que las unicas plazas que quedan son reservadas -para
        personas con movilidad reducida o para viajeros con bicicleta-. Un
        usuario sin esa necesidad no puede comprarlas en la practica, asi que
        para el caso de uso de TrainPicker (avisar cuando se libera una plaza
        normal) se trata como tren completo. Se comprueban las dos señales
        porque no siempre coinciden: verificado en vivo, `soloPlazaH` de nivel
        superior puede venir en `false` aunque la unica tarifa ofertada sea de
        plaza bicicleta (12 de 794 itinerarios reales muestreados en 3 rutas y
        21 fechas), mientras que el anidado nunca se ha visto en `false`
        cuando el de nivel superior esta en `true`.

    Fail-closed ante roturas de formato: si ninguna de las cuatro señales
    aparece, se asume que Renfe cambio el formato del DWR y se marca como NO
    disponible (en vez de disponible por defecto, tarea #5).
    """
    if not any(name in fields for name in _AVAILABILITY_FIELDS):
        return False, "formato_desconocido"

    motivos: List[str] = []

    if _bool_field(fields, "completo"):
        motivos.append("completo")

    if fields.get("tarifasDisponibles") == "null":
        motivos.append("sin_tarifas")

    razon = _text_field(fields, "razonNoDisponible")
    if razon not in (None, "", "8"):
        motivos.append("razon_%s" % razon)

    if _bool_field(fields, "soloPlazaH") or fields.get("_tarifaSoloPlazaReservada"):
        motivos.append("solo_plaza_reservada")

    if not motivos:
        return True, None

    return False, "+".join(motivos)


def parsear_dwr_renfe(texto_dwr: str, date_str: str) -> List[Dict[str, Any]]:
    """Parsea la respuesta DWR de Renfe y devuelve los trenes de `date_str`.

    Cada itinerario se identifica por `(codigosTrenTramo, salida, llegada,
    origen, destino)`, donde `codigosTrenTramo` son los `cdgoTren` reales de
    `trayectos` (ver `_codigos_tren_tramo`) -el itinerario en si no trae un
    `cdgoTren` propio, asi que leerlo como campo de primer nivel siempre da
    `None` y deja la identidad reducida a salida/llegada/origen/destino sin
    avisar-. Antes se indexaba solo por la hora de salida y se aplicaba un OR
    de disponibilidad, asi que dos itinerarios distintos que salen a la misma
    hora -habituales en esta respuesta, que es la de `trainEnlacesManager` y
    mezcla trenes directos con enlaces y acercamientos- se fundian en uno y la
    plaza libre de uno marcaba como disponible el que estaba completo (issue
    #25). Eso incluye pares de trenes reales y distintos con identica salida y
    llegada nominal (visto en vivo: 13083 con plaza y 35083 "Tren Completo",
    ambos San Bernardo 18:12 -> Puerto de Santa Maria 19:26 del 13/09/2026). El
    OR se mantiene, pero solo entre bloques que son literalmente el mismo tren
    (varias filas de tarifa del mismo itinerario).

    Ademas de `disponible` (contrato estable que consumen `main.py` y
    `scheduler.py`), se exponen campos del itinerario -tren, duracion, precio
    orientativo, si es directo, disponibilidad por tipo de plaza- y `motivo`,
    que explica por que un tren se ha marcado como no disponible.
    """
    trenes_unicos: Dict[Tuple[str, ...], Dict[str, Any]] = {}
    fechas_vistas: set = set()

    try:
        d, m, y = date_str.split('/')
        target_date = f"{y}-{m}-{d}"
    except ValueError:
        logger.error("parsear_dwr_renfe: fecha invalida %r (se espera DD/MM/AAAA)", date_str)
        return []

    itinerarios = _iter_itinerary_fields(texto_dwr)

    for fields in itinerarios:
        try:
            fecha_itinerario = _text_field(fields, "fecha")
            fechas_vistas.add(fecha_itinerario)
            if fecha_itinerario != target_date:
                continue

            salida = _text_field(fields, "horaSalida")
            llegada = _text_field(fields, "horaLlegada")
            if not salida or not llegada:
                continue

            origen_real = _text_field(fields, "descripcionEstacionOrigen") or ""
            destino_real = _text_field(fields, "descripcionEstacionDestino") or ""
            codigo_tren = fields.get("_codigosTrenTramo") or None

            disponible, motivo = evaluar_disponibilidad(fields)

            if motivo == "formato_desconocido":
                logger.warning(
                    "parsear_dwr_renfe: no se encontro ninguna señal de disponibilidad "
                    "(completo/tarifasDisponibles/razonNoDisponible/soloPlazaH) para el "
                    "tren %s (salida %s, %s). Renfe pudo cambiar el formato del DWR; se "
                    "marca como no disponible por seguridad.",
                    codigo_tren or "?", salida, target_date,
                )

            tren_data = {
                "salida": salida,
                "llegada": llegada,
                "origen": origen_real.title(),
                "destino": destino_real.title(),
                "disponible": disponible,
                "motivo": motivo,
                "tren": codigo_tren,
                "duracion": _text_field(fields, "duracionViaje"),
                "precio_desde": _text_field(fields, "tarifaMinima"),
                "directo": _bool_field(fields, "directo"),
                "plaza_h_disponible": _bool_field(fields, "plazaHDisponible"),
                "plaza_b_disponible": _bool_field(fields, "plazaBDisponible"),
            }

            identidad = (codigo_tren or "", salida, llegada, origen_real, destino_real)
            existente = trenes_unicos.get(identidad)

            if existente is None:
                trenes_unicos[identidad] = tren_data
            elif tren_data["disponible"] and not existente["disponible"]:
                existente["disponible"] = True
                existente["motivo"] = None

        except Exception as e:
            logger.exception("Error parseando un itinerario del DWR: %s", e)

    if itinerarios and not trenes_unicos:
        # Renfe respondio con itinerarios pero ninguno es de la fecha pedida.
        # Puede ser normal (no hay trenes ese dia), pero tambien seria el
        # sintoma de que `fecha` ha dejado de ser un campo del itinerario: en
        # ese caso el bot diria "no se han encontrado trenes" para todo, en
        # silencio. Se deja rastro con las fechas que si venian.
        logger.warning(
            "parsear_dwr_renfe: %s itinerarios en la respuesta y ninguno para %s "
            "(fechas encontradas: %s)",
            len(itinerarios), target_date,
            ", ".join(sorted(f for f in fechas_vistas if f)) or "ninguna",
        )

    return sorted(trenes_unicos.values(), key=lambda tren: (tren["salida"], tren["llegada"]))


async def _fetch_with_cached_session(search_key: str, date_str: str) -> Optional[List[Dict[str, Any]]]:
    session = get_session_cache(search_key)
    if not session:
        return None

    payload = session["post_data"]
    if payload is None:
        payload = b""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")

    clean_headers = _sanitize_headers(session["headers"])

    async with httpx.AsyncClient() as client:
        if session["method"] == "POST":
            res = await client.post(
                session["url"],
                headers=clean_headers,
                content=payload,
                timeout=10.0,
            )
        else:
            res = await client.get(session["url"], headers=clean_headers, timeout=10.0)

    if res.status_code == 200 and "handleCallback" in res.text:
        logger.info("Respuesta API directa exitosa")
        return parsear_dwr_renfe(res.text, date_str)

    logger.warning("Sesion caducada (HTTP %s). Renovando token", res.status_code)
    return None


_playwright_instance: Optional[Playwright] = None
_browser: Optional[Browser] = None
# Cache en memoria del storage_state; None mientras no se haya cargado ni
# capturado nada en este proceso (se rellena de disco de forma perezosa).
_storage_state: Optional[Dict[str, Any]] = None
_storage_state_loaded: bool = False
_browser_lock: Optional[asyncio.Lock] = None


def _cookies_only(state: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Reduce un storage_state de Playwright a solo sus cookies.

    Descarta `origins` (localStorage/sessionStorage), que es la parte que puede
    hincharse sin control y que no necesitamos para replicar la sesion.
    """
    if not state:
        return None
    cookies = state.get("cookies") or []
    if not cookies:
        return None
    return {"cookies": cookies, "origins": []}


def _load_storage_state_from_disk() -> Optional[Dict[str, Any]]:
    """Carga el storage_state persistido, o None si no hay o esta corrupto."""
    try:
        with open(STORAGE_STATE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("No se pudo leer %s (%s); se ignora", STORAGE_STATE_PATH, exc)
        return None
    return _cookies_only(data)


def _persist_storage_state(state: Optional[Dict[str, Any]]) -> None:
    """Guarda en disco solo las cookies del storage_state, de forma atomica.

    Se aplica el tope `STORAGE_STATE_MAX_BYTES`: si se supera, no se persiste
    (se prefiere volver a hacer cola a llenar el volumen). Cualquier fallo de
    E/S es best-effort y no interrumpe la captura.
    """
    reducido = _cookies_only(state)
    if reducido is None:
        return

    try:
        payload = json.dumps(reducido, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        logger.debug("storage_state no serializable (%s); no se persiste", exc)
        return

    if len(payload.encode("utf-8")) > STORAGE_STATE_MAX_BYTES:
        logger.warning(
            "storage_state supera %s bytes; no se persiste para no hinchar el volumen",
            STORAGE_STATE_MAX_BYTES,
        )
        return

    try:
        os.makedirs(os.path.dirname(STORAGE_STATE_PATH) or ".", exist_ok=True)
        directorio = os.path.dirname(STORAGE_STATE_PATH) or "."
        fd, tmp = tempfile.mkstemp(dir=directorio, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, STORAGE_STATE_PATH)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    except OSError as exc:
        logger.warning("No se pudo persistir storage_state (%s)", exc)


def _get_browser_lock() -> asyncio.Lock:
    # Instanciado de forma perezosa (no a nivel de modulo) para no atarlo al
    # primer event loop que exista en el proceso de importacion: crear un
    # asyncio.Lock() en la carga del modulo puede quedar ligado a un loop
    # distinto del que usa `asyncio.run(main())`. No hay punto de suspension
    # entre el check y la asignacion, asi que es seguro sin lock adicional.
    global _browser_lock
    if _browser_lock is None:
        _browser_lock = asyncio.Lock()
    return _browser_lock


async def _get_browser() -> Browser:
    """Devuelve el Browser compartido a nivel de proceso, lanzandolo si hace falta.

    Arrancar Chromium cuesta ~1-2s; reutilizar una unica instancia entre
    capturas (cada una abre su propio `context`/`page`, que si se cierran)
    evita pagar ese coste en cada refresco de sesion.
    """
    global _playwright_instance, _browser

    async with _get_browser_lock():
        if _browser is None or not _browser.is_connected():
            if _playwright_instance is None:
                _playwright_instance = await async_playwright().start()
            _browser = await _playwright_instance.chromium.launch(headless=True)

    return _browser


async def close_browser() -> None:
    """Cierra el Browser compartido y el proceso de Playwright, si estan activos.

    Pensado para el shutdown de `main.py`/`scheduler.py`; no es obligatorio
    llamarlo (el proceso se lleva el navegador por delante al terminar), pero
    evita dejar el proceso de Chromium huerfano en un apagado ordenado.
    """
    global _playwright_instance, _browser

    async with _get_browser_lock():
        if _browser is not None:
            await _browser.close()
            _browser = None
        if _playwright_instance is not None:
            await _playwright_instance.stop()
            _playwright_instance = None


async def _seleccionar_estacion(page, campo: str, texto: str) -> None:
    """Escribe `texto` en el autocompletado `campo` y confirma la sugerencia.

    Hay que CLICAR el `<li>`: Renfe dejo de confirmar la seleccion con
    ArrowDown+Enter (la sugerencia se queda en `aria-selected="false"`). Sin
    confirmar, el hidden `cdgoOrigen`/`cdgoDestino` se queda vacio y el boton
    "Buscar billete" nunca sale de `disabled`, asi que el `click` posterior se
    quedaba esperando a que se habilitase hasta agotar el timeout: no se
    llegaba a lanzar ninguna peticion DWR y el bot contestaba "No se han
    encontrado trenes. Revisa los nombres de las estaciones" para todo.

    Por eso se verifica el hidden despues de clicar: si Renfe vuelve a cambiar
    el autocompletado, el fallo sale aqui -con nombre- en vez de disfrazarse de
    busqueda sin resultados medio minuto despues.
    """
    selector = f"input#{campo}"
    opcion = f"#{campo}-awe li[role='option']"
    hidden = "cdgoOrigen" if campo == "origin" else "cdgoDestino"

    await page.click(selector)
    await page.fill(selector, "")
    await page.locator(selector).press_sequentially(texto, delay=AUTOCOMPLETE_TYPE_DELAY_MS)
    await page.wait_for_selector(opcion, state="visible", timeout=5000)
    await page.locator(opcion).first.click()

    # Renfe rellena el hidden ~100-500ms DESPUES del click, no dentro del
    # manejador: leerlo al vuelo devuelve siempre vacio y daria un falso fallo.
    leer_hidden = (
        "(name) => { const el = document.querySelector(`input[name=\"${name}\"]`);"
        " return el ? el.value : ''; }"
    )
    try:
        await page.wait_for_function(
            f"(name) => {{ const v = ({leer_hidden})(name); return !!v; }}",
            arg=hidden,
            timeout=STATION_CONFIRM_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError as exc:
        raise EstacionNoConfirmadaError(
            f"Renfe no confirmo la estacion {texto!r} en el campo {campo!r}: "
            f"{hidden} sigue vacio {STATION_CONFIRM_TIMEOUT_MS}ms despues de "
            f"clicar la sugerencia"
        ) from exc

    codigo = await page.evaluate(leer_hidden, hidden)
    logger.debug("Estacion %s confirmada en %s (%s=%s)", texto, campo, hidden, codigo)


_COLA_PERSONAS_RE = re.compile(r"(\d+)\s*personas?\s*delante", re.IGNORECASE)
_COLA_MINUTOS_RE = re.compile(r"(\d+)\s*minutos?\s*de\s*tiempo\s*de\s*espera", re.IGNORECASE)


async def _leer_estado_cola(page) -> Dict[str, Any]:
    """Extrae posicion y espera estimada de la pagina de Queue-it.

    Best-effort: es la web de un tercero y solo sirve para dar contexto al
    usuario, asi que cualquier fallo devuelve los campos a None en vez de
    romper la captura.
    """
    estado: Dict[str, Any] = {"url": page.url, "personas_delante": None, "minutos": None}
    try:
        texto = await page.evaluate("() => document.body.innerText")
    except Exception:
        return estado

    personas = _COLA_PERSONAS_RE.search(texto)
    minutos = _COLA_MINUTOS_RE.search(texto)
    if personas:
        estado["personas_delante"] = int(personas.group(1))
    if minutos:
        estado["minutos"] = int(minutos.group(1))
    return estado


async def _esperar_respuesta_dwr(
    page,
    url_keyword: str,
    lanzar_busqueda,
    on_queue: Optional[AvisoColaCallback] = None,
):
    """Lanza la busqueda y espera la respuesta DWR, tolerando la cola de Renfe.

    Cuando Renfe esta saturada mete la busqueda en una sala de espera virtual
    (`renfe.queue-it.net`) antes de dejarte llegar a `venta.renfe.com`. Mientras
    estamos en la cola NO se dispara ninguna peticion DWR, asi que la espera
    corta vencia y el bot lo reportaba como "no hay trenes". Aqui se espera el
    turno como un usuario normal: espera corta por defecto y, solo si estamos
    realmente en la cola, se amplia hasta `QUEUE_TIMEOUT_S`.

    El listener se registra ANTES de lanzar la busqueda para no perder la
    respuesta si llega muy rapido.

    Si se pasa `on_queue`, se invoca UNA vez al detectar la cola (con la
    posicion y la espera estimada) para poder avisar al usuario de que la
    espera es de Renfe y no un cuelgue del bot.
    """
    futuro: "asyncio.Future" = asyncio.get_running_loop().create_future()

    def _on_response(response) -> None:
        if futuro.done():
            return
        if url_keyword in response.url and response.status == 200:
            futuro.set_result(response)

    page.on("response", _on_response)
    try:
        await lanzar_busqueda()

        try:
            return await asyncio.wait_for(asyncio.shield(futuro), timeout=DWR_TIMEOUT_S)
        except asyncio.TimeoutError:
            if QUEUE_HOST not in page.url:
                raise

        estado = await _leer_estado_cola(page)
        logger.warning(
            "Renfe nos ha puesto en su cola virtual (%s personas delante, ~%s min). "
            "Esperando turno hasta %ss",
            estado["personas_delante"], estado["minutos"], QUEUE_TIMEOUT_S,
        )
        if on_queue is not None:
            # Un aviso fallido (p.ej. Telegram caido) no puede tumbar la captura:
            # seguimos esperando turno igual.
            try:
                await on_queue(estado)
            except Exception:
                logger.exception("No se pudo avisar de la cola de Renfe")

        try:
            return await asyncio.wait_for(asyncio.shield(futuro), timeout=QUEUE_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise RenfeEnColaError(
                f"Seguimos en la cola virtual de Renfe tras {QUEUE_TIMEOUT_S}s ({page.url})"
            )
    finally:
        page.remove_listener("response", _on_response)


async def _capture_session_with_playwright(
    origin: str,
    destination: str,
    date_str: str,
    search_key: str,
    on_queue: Optional[AvisoColaCallback] = None,
) -> List[Dict[str, Any]]:
    global _consecutive_capture_failures, _storage_state, _storage_state_loaded

    logger.info("Iniciando Playwright para capturar sesion de Renfe")

    # Primera captura del proceso: intenta rehidratar las cookies persistidas
    # en disco para no volver a hacer cola tras un reinicio del contenedor.
    if not _storage_state_loaded:
        _storage_state_loaded = True
        if _storage_state is None:
            _storage_state = _load_storage_state_from_disk()
            if _storage_state:
                logger.info("storage_state rehidratado desde %s", STORAGE_STATE_PATH)

    browser = await _get_browser()
    # Se reutilizan las cookies de la captura anterior. Ademas de la sesion de
    # Renfe, eso conserva el pase de la cola virtual (Queue-it da ~20 min de
    # ventana): con un contexto nuevo por captura haciamos cola otra vez en
    # CADA ruta que refresca el scheduler.
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
        storage_state=_storage_state,
    )
    page = await context.new_page()

    await page.route("**/*", lambda route: route.continue_() if route.request.resource_type in ALLOWED_RESOURCE_TYPES else route.abort())

    try:
        logger.info("Buscando trenes: %s -> %s el %s", origin, destination, date_str)
        await page.goto("https://www.renfe.com/es/es", timeout=60000)

        try:
            await page.click("button#onetrust-accept-btn-handler", timeout=5000)
        except Exception:
            pass

        await _seleccionar_estacion(page, "origin", origin)
        await _seleccionar_estacion(page, "destination", destination)

        # El radio "solo ida" (label[for='trip-go']) vive dentro del widget de
        # calendario ("lightpick"), que solo se monta/muestra al abrir el
        # campo de fecha de ida (#first-input). Intentar clicarlo antes de
        # abrir el calendario (como se hacia antes) lo deja siempre oculto,
        # lo que quema el timeout completo de 5s en cada captura antes de
        # caer al fallback por JS. Abrir el calendario primero lo deja
        # clicable de verdad y evita ese coste.
        await page.click("input#first-input")
        try:
            await page.wait_for_selector("label[for='trip-go']", state="visible", timeout=3000)
            await page.click("label[for='trip-go']")
        except Exception:
            await page.evaluate(
                """
                () => {
                    const radio = document.querySelector("input#trip-go");
                    if (!radio) return;
                    radio.checked = true;
                    radio.dispatchEvent(new Event('input', { bubbles: true }));
                    radio.dispatchEvent(new Event('change', { bubbles: true }));
                    radio.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                }
                """
            )

        fecha_asignada = await page.evaluate(
                """
                (value) => {
                    const m = /^(\d{2})\/(\d{2})\/(\d{4})$/.exec(value || '');
                    if (!m) return { ok: false, reason: 'Formato de fecha inválido' };

                    const dd = m[1]; const mm = m[2]; const yyyy = m[3];
                    const iso = `${yyyy}-${mm}-${dd}`;
                    const compact = `${yyyy}${mm}${dd}`;

                    const containsAny = (txt, keys) => keys.some((k) => txt.includes(k));

                    const isDepartureField = (el) => {
                        const bag = [
                            el.id || '', el.name || '', el.className || '',
                            el.getAttribute('aria-label') || '', el.getAttribute('placeholder') || '',
                            el.getAttribute('title') || '',
                        ].join(' ').toLowerCase();
                        const depKeys = ['first-input', 'ida', 'departure', 'salida', 'outbound', 'going', 'dategone'];
                        const retKeys = ['second-input', 'vuelta', 'return', 'round'];
                        return containsAny(bag, depKeys) && !containsAny(bag, retKeys);
                    };

                    const allRoots = [document];
                    const rootQueue = [document];
                    while (rootQueue.length) {
                        const root = rootQueue.shift();
                        const nodes = Array.from(root.querySelectorAll('*'));
                        for (const node of nodes) {
                            if (node.shadowRoot) {
                                allRoots.push(node.shadowRoot);
                                rootQueue.push(node.shadowRoot);
                            }
                        }
                    }

                    const candidates = [];
                    for (const root of allRoots) {
                        const inputs = Array.from(root.querySelectorAll('input'));
                        for (const input of inputs) {
                            if (isDepartureField(input)) candidates.push(input);
                        }
                    }

                    let updated = 0;
                    for (const input of candidates) {
                        try {
                            const type = (input.type || '').toLowerCase();
                            const useIso = type === 'date';
                            const nextValue = useIso ? iso : value;

                            input.removeAttribute('readonly');
                            input.value = nextValue;
                            input.setAttribute('value', nextValue);
                            input.dispatchEvent(new Event('input', { bubbles: true }));
                            input.dispatchEvent(new Event('change', { bubbles: true }));
                            input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', bubbles: true }));
                            input.dispatchEvent(new Event('blur', { bubbles: true }));
                            updated += 1;
                        } catch (_) {}
                    }

                    for (const root of allRoots) {
                        const hiddenInputs = Array.from(root.querySelectorAll('input[type="hidden"]'));
                        for (const hidden of hiddenInputs) {
                            if (!isDepartureField(hidden)) continue;
                            const typeHint = `${hidden.id || ''} ${hidden.name || ''}`.toLowerCase();
                            if (typeHint.includes('iso') || typeHint.includes('yyyy')) {
                                hidden.value = iso;
                            } else if (typeHint.includes('compact') || typeHint.includes('yyyymmdd')) {
                                hidden.value = compact;
                            } else {
                                hidden.value = value;
                            }
                            hidden.setAttribute('value', hidden.value);
                            hidden.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                    }

                    return { ok: updated > 0, updated };
                }
                """,
                date_str,
            )

        if not fecha_asignada.get("ok"):
            raise Exception("No se pudo aplicar la fecha de ida en el formulario")

        logger.info("Interceptando solicitud DWR de trenes")
        url_keyword = "getTrainsList.dwr"

        async def _lanzar_busqueda() -> None:
            search_button = "button[title='Buscar billete']"
            await page.wait_for_selector(search_button, state="visible", timeout=10000)
            await page.click(search_button)

        api_response = await _esperar_respuesta_dwr(
            page, url_keyword, _lanzar_busqueda, on_queue=on_queue
        )
        texto_dwr = await api_response.text()

        api_request = api_response.request
        upsert_session_cache(
            search_key=search_key,
            url=api_request.url,
            method=api_request.method,
            headers=await api_request.all_headers(),
            post_data=api_request.post_data,
        )
        logger.info("Sesion y tokens de Renfe cacheados con exito")

        _consecutive_capture_failures = 0

        return parsear_dwr_renfe(texto_dwr, date_str)

    except Exception as e:
        _consecutive_capture_failures += 1
        logger.exception("Error durante captura de sesion DWR: %s", e)
        try:
            await page.screenshot(path="error_renfe.png", full_page=True)
        except Exception:
            logger.debug("No se pudo guardar la captura de pantalla del error")
        # Antes se devolvia [], indistinguible de "no hay trenes": el bot
        # respondia "Revisa los nombres de las estaciones" aunque el fallo
        # fuese nuestro o la cola de Renfe. Los llamantes ya capturan.
        raise

    finally:
        try:
            _storage_state = await context.storage_state()
            # Persistir en disco (solo cookies, con tope) para que el pase de la
            # cola sobreviva a un reinicio del contenedor.
            _persist_storage_state(_storage_state)
        except Exception:
            logger.debug("No se pudo guardar el storage_state del contexto")
        await context.close()


async def get_trains(
    origin: str,
    destination: str,
    date_str: str,
    on_queue: Optional[AvisoColaCallback] = None,
) -> List[Dict[str, Any]]:
    """Consulta trenes: intenta la sesion cacheada y si falla, cae a Playwright.

    Pensada para el flujo de un solo usuario (alta de alerta desde `main.py`),
    donde bloquear unos segundos en el fallback lento es aceptable. El
    scheduler NO debe usar esta funcion para su barrido periodico: usa
    `get_trains_cached_only` (rapido, sin Playwright) y `refresh_session`
    (Playwright, acotado por semaforo) para no acoplar el intervalo de todas
    las rutas a la mas lenta.

    `on_queue` se invoca si Renfe nos mete en su cola virtual, para poder
    avisar por Telegram de que la espera puede irse a varios minutos.
    """
    search_key = build_search_key(origin, destination, date_str)

    logger.info("Intentando usar sesion cacheada para %s", search_key)
    try:
        cached_result = await _fetch_with_cached_session(search_key, date_str)
        if cached_result is not None:
            return cached_result
        delete_session_cache(search_key)
    except Exception as e:
        logger.exception("Error en llamada API directa: %s", e)
        delete_session_cache(search_key)

    return await _capture_session_with_playwright(
        origin, destination, date_str, search_key, on_queue=on_queue
    )


async def get_trains_cached_only(
    origin: str, destination: str, date_str: str
) -> Optional[List[Dict[str, Any]]]:
    """Consulta trenes usando SOLO la sesion cacheada, sin abrir Playwright.

    Pensada para el job rapido del scheduler: si no hay cache o la sesion ha
    caducado, devuelve None de inmediato (borrando el cache caducado para que
    `refresh_sessions` la recapture) en vez de bloquear ese ciclo con el
    fallback lento de Playwright.
    """
    search_key = build_search_key(origin, destination, date_str)

    try:
        cached_result = await _fetch_with_cached_session(search_key, date_str)
        if cached_result is not None:
            return cached_result
    except Exception as e:
        logger.exception("Error en llamada API directa (cache-only) para %s: %s", search_key, e)

    delete_session_cache(search_key)
    return None


async def refresh_session(origin: str, destination: str, date_str: str) -> List[Dict[str, Any]]:
    """Recaptura la sesion de Renfe via Playwright y cachea el resultado.

    Pensada para el job lento del scheduler (`refresh_sessions`), que la
    invoca solo para las rutas sin cache valido y acotado por un semaforo
    para no abrir demasiados navegadores a la vez.
    """
    search_key = build_search_key(origin, destination, date_str)
    return await _capture_session_with_playwright(origin, destination, date_str, search_key)