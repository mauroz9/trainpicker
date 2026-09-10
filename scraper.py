import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx
from playwright.async_api import Browser, Playwright, async_playwright

from database import build_search_key, delete_session_cache, get_session_cache, upsert_session_cache

logger = logging.getLogger(__name__)
ALLOWED_RESOURCE_TYPES = ["document", "script", "xhr", "fetch"]
AUTOCOMPLETE_TYPE_DELAY_MS = 60

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


def _extract_object_fields(text: str, start: int) -> Dict[str, str]:
    """Extrae los campos del objeto que empieza en `start`, sin entrar en los anidados.

    `start` apunta al valor de `acercamientoViajeDestino`, es decir, al interior
    del objeto del itinerario. Se leen sus campos hasta el `}` que lo cierra;
    todo objeto o array anidado (tarifas, tramos de un enlace) se salta entero,
    de forma que nunca aporta un `completo:`/`horaSalida:`/`fecha:` que no le
    corresponde al itinerario. Los valores se devuelven crudos (`"3"`, `null`,
    `true`, `[`), tal cual aparecen en el DWR.
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

    return fields


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
    return [
        _extract_object_fields(texto_dwr, start)
        for depth, start in occurrences
        if depth == top_depth
    ]


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
      - `soloPlazaH == true`: en el frontend de Renfe esto NO bloquea la compra
        (solo cambia que plantilla/icono se pinta), pero significa que las
        unicas plazas que quedan son plazas H, reservadas para personas con
        movilidad reducida. Un usuario sin esa necesidad no puede comprarlas en
        la practica, asi que para el caso de uso de TrainPicker (avisar cuando
        se libera una plaza normal) se trata como tren completo.

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

    if _bool_field(fields, "soloPlazaH"):
        motivos.append("solo_plaza_h")

    if not motivos:
        return True, None

    return False, "+".join(motivos)


def parsear_dwr_renfe(texto_dwr: str, date_str: str) -> List[Dict[str, Any]]:
    """Parsea la respuesta DWR de Renfe y devuelve los trenes de `date_str`.

    Cada itinerario se identifica por `(cdgoTren, salida, llegada, origen,
    destino)`. Antes se indexaba solo por la hora de salida y se aplicaba un OR
    de disponibilidad, asi que dos itinerarios distintos que salen a la misma
    hora -habituales en esta respuesta, que es la de `trainEnlacesManager` y
    mezcla trenes directos con enlaces y acercamientos- se fundian en uno y la
    plaza libre de uno marcaba como disponible el que estaba completo (issue
    #25). El OR se mantiene, pero solo entre bloques que son literalmente el
    mismo tren (varias filas de tarifa del mismo itinerario).

    Ademas de `disponible` (contrato estable que consumen `main.py` y
    `scheduler.py`), se exponen campos del itinerario -tren, duracion, precio
    orientativo, si es directo, disponibilidad por tipo de plaza- y `motivo`,
    que explica por que un tren se ha marcado como no disponible.
    """
    trenes_unicos: Dict[Tuple[str, ...], Dict[str, Any]] = {}

    try:
        d, m, y = date_str.split('/')
        target_date = f"{y}-{m}-{d}"
    except ValueError:
        logger.error("parsear_dwr_renfe: fecha invalida %r (se espera DD/MM/AAAA)", date_str)
        return []

    for fields in _iter_itinerary_fields(texto_dwr):
        try:
            if _text_field(fields, "fecha") != target_date:
                continue

            salida = _text_field(fields, "horaSalida")
            llegada = _text_field(fields, "horaLlegada")
            if not salida or not llegada:
                continue

            origen_real = _text_field(fields, "descripcionEstacionOrigen") or ""
            destino_real = _text_field(fields, "descripcionEstacionDestino") or ""
            codigo_tren = _text_field(fields, "cdgoTren")

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
_browser_lock: Optional[asyncio.Lock] = None


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


async def _capture_session_with_playwright(
    origin: str,
    destination: str,
    date_str: str,
    search_key: str,
) -> List[Dict[str, Any]]:
    global _consecutive_capture_failures

    logger.info("Iniciando Playwright para capturar sesion de Renfe")

    browser = await _get_browser()
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"
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

        await page.click("input#origin")
        await page.fill("input#origin", "")
        await page.locator("input#origin").press_sequentially(origin, delay=AUTOCOMPLETE_TYPE_DELAY_MS)
        await page.wait_for_selector("#origin-awe li[role='option']", state="visible", timeout=5000)
        await page.keyboard.press("ArrowDown")
        await page.keyboard.press("Enter")

        await page.click("input#destination")
        await page.fill("input#destination", "")
        await page.locator("input#destination").press_sequentially(destination, delay=AUTOCOMPLETE_TYPE_DELAY_MS)
        await page.wait_for_selector("#destination-awe li[role='option']", state="visible", timeout=5000)
        await page.keyboard.press("ArrowDown")
        await page.keyboard.press("Enter")

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

        async with page.expect_response(lambda response: url_keyword in response.url and response.status == 200, timeout=30000) as response_info:
            search_button = "button[title='Buscar billete']"
            await page.wait_for_selector(search_button, state="visible", timeout=10000)
            await page.click(search_button)

        api_response = await response_info.value
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
        await page.screenshot(path="error_renfe.png", full_page=True)
        return []

    finally:
        await context.close()


async def get_trains(origin: str, destination: str, date_str: str) -> List[Dict[str, Any]]:
    """Consulta trenes: intenta la sesion cacheada y si falla, cae a Playwright.

    Pensada para el flujo de un solo usuario (alta de alerta desde `main.py`),
    donde bloquear unos segundos en el fallback lento es aceptable. El
    scheduler NO debe usar esta funcion para su barrido periodico: usa
    `get_trains_cached_only` (rapido, sin Playwright) y `refresh_session`
    (Playwright, acotado por semaforo) para no acoplar el intervalo de todas
    las rutas a la mas lenta.
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

    return await _capture_session_with_playwright(origin, destination, date_str, search_key)


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