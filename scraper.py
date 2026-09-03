import asyncio
import codecs
import logging
import re
from typing import Any, Dict, List, Optional

import httpx
from playwright.async_api import Browser, Playwright, async_playwright

from database import build_search_key, delete_session_cache, get_session_cache, upsert_session_cache

logger = logging.getLogger(__name__)
ALLOWED_RESOURCE_TYPES = ["document", "script", "xhr", "fetch"]
AUTOCOMPLETE_TYPE_DELAY_MS = 60


def _sanitize_headers(headers: Dict[str, str]) -> Dict[str, str]:
    return {
        key: value.encode("ascii", "ignore").decode("ascii")
        for key, value in headers.items()
    }


def _decode_escaped_text(value: str) -> str:
    return codecs.decode(value, "unicode_escape")

def parsear_dwr_renfe(texto_dwr: str, date_str: str) -> List[Dict[str, Any]]:
    """
    Parsea la respuesta DWR de Renfe.

    Disponibilidad: parte de la logica real que usa el propio frontend de
    Renfe (`listaTrenes.js`, funcion que decide entre las plantillas
    `trenTemplateCompleto` / `trenTemplateBloqueado` / `trenTemplateNoCircula`
    / `trenTemplateNoVenta`) para decidir si un tren es comprable, mas una
    restriccion adicional de negocio propia de TrainPicker. Un tren se
    considera NO disponible si se cumple cualquiera de:
      - `completo == true`.
      - `razonNoDisponible` esta presente, no vacio y distinto de `"8"`
        (Renfe usa el codigo "8" solo para incidencias informativas -p.ej.
        limitaciones de velocidad de Adif- que no bloquean la venta; el resto
        de codigos observados -"3" completo, "4" trayecto bloqueado, "5"/"6"/
        "7" no circula- si la bloquean, igual que cualquier codigo nuevo no
        catalogado, replicando el `else` final de esa misma funcion).
      - `tarifasDisponibles == null` (sin tarifas no hay nada que comprar).
      - `soloPlazaH == true`: en el frontend de Renfe esto NO bloquea el
        flujo de compra (solo cambia que plantilla/icono se pinta), pero
        significa que las unicas plazas que quedan son plazas H, reservadas
        para personas con movilidad reducida. Un usuario sin esa necesidad
        no puede comprarlas en la practica, asi que para el caso de uso de
        TrainPicker (avisar cuando se libera una plaza normal) se trata como
        tren completo.
    Ver PR de la tarea #5 para el analisis con datos reales y el volcado del
    JS de Renfe que respalda esta logica.

    Fail-closed ante roturas de formato: si ninguna de las cuatro señales
    (`completo`, `tarifasDisponibles`, `razonNoDisponible`, `soloPlazaH`)
    matchea en un bloque, se asume que Renfe cambio el formato del DWR. En
    vez de marcar el tren como disponible por defecto (fail-open silencioso,
    tarea #5), se marca como NO disponible y se loguea un warning explicito
    para detectar la rotura cuanto antes.

    Ademas de `disponible` (contrato estable que consumen `main.py` y
    `scheduler.py`), se exponen campos adicionales del bloque -tren, duracion,
    precio orientativo, si es directo y disponibilidad por tipo de plaza- para
    dar contexto mas rico sin romper lo existente.
    """
    trenes_unicos: Dict[str, Dict[str, Any]] = {}

    try:
        d, m, y = date_str.split('/')
        target_date = f"{y}-{m}-{d}"

        bloques = texto_dwr.split('acercamientoViajeDestino:')

        for bloque in bloques[1:]:
            fecha_m = re.search(r'fecha:\s*"([^"]+)"', bloque)
            if not fecha_m or fecha_m.group(1) != target_date:
                continue

            salida_m = re.search(r'horaSalida:\s*"(\d{2}:\d{2})"', bloque)
            llegada_m = re.search(r'horaLlegada:\s*"(\d{2}:\d{2})"', bloque)

            origen_m = re.search(r'descripcionEstacionOrigen:\s*"([^"]+)"', bloque)
            destino_m = re.search(r'descripcionEstacionDestino:\s*"([^"]+)"', bloque)

            completo_m = re.search(r'completo:\s*(true|false)', bloque)
            tarifas_m = re.search(r'tarifasDisponibles:\s*(null|\[)', bloque)
            razon_m = re.search(r'razonNoDisponible:\s*(null|"[^"]*")', bloque)
            solo_plazah_m = re.search(r'soloPlazaH:\s*(true|false)', bloque)

            tren_m = re.search(r'cdgoTren:\s*"([^"]*)"', bloque)
            duracion_m = re.search(r'duracionViaje:\s*"([^"]*)"', bloque)
            precio_m = re.search(r'tarifaMinima:\s*(null|"[^"]*")', bloque)
            directo_m = re.search(r'directo:\s*(true|false)', bloque)
            plaza_h_m = re.search(r'plazaHDisponible:\s*(true|false)', bloque)
            plaza_b_m = re.search(r'plazaBDisponible:\s*(true|false)', bloque)

            if salida_m and llegada_m:
                salida = salida_m.group(1)
                llegada = llegada_m.group(1)

                origen_real = _decode_escaped_text(origen_m.group(1)) if origen_m else ""
                destino_real = _decode_escaped_text(destino_m.group(1)) if destino_m else ""

                if not (completo_m or tarifas_m or razon_m or solo_plazah_m):
                    logger.warning(
                        "parsear_dwr_renfe: no se encontro ninguna señal de disponibilidad "
                        "(completo/tarifasDisponibles/razonNoDisponible/soloPlazaH) para el "
                        "tren %s (salida %s, %s). Renfe pudo cambiar el formato del DWR; se "
                        "marca como no disponible por seguridad.",
                        tren_m.group(1) if tren_m else "?", salida, target_date,
                    )
                    is_full = True
                else:
                    is_full = False

                    if completo_m and completo_m.group(1) == 'true':
                        is_full = True

                    if tarifas_m and tarifas_m.group(1) == 'null':
                        is_full = True

                    if razon_m and razon_m.group(1) not in ('null', '"8"'):
                        is_full = True

                    if solo_plazah_m and solo_plazah_m.group(1) == 'true':
                        is_full = True

                tren_data = {
                    "salida": salida,
                    "llegada": llegada,
                    "origen": origen_real.title(),
                    "destino": destino_real.title(),
                    "disponible": not is_full,
                    "tren": tren_m.group(1) if tren_m else None,
                    "duracion": duracion_m.group(1) if duracion_m else None,
                    "precio_desde": precio_m.group(1).strip('"') if precio_m and precio_m.group(1) != 'null' else None,
                    "directo": directo_m.group(1) == 'true' if directo_m else None,
                    "plaza_h_disponible": plaza_h_m.group(1) == 'true' if plaza_h_m else None,
                    "plaza_b_disponible": plaza_b_m.group(1) == 'true' if plaza_b_m else None,
                }

                existente = trenes_unicos.get(salida)
                if existente is None or (not existente["disponible"] and tren_data["disponible"]):
                    trenes_unicos[salida] = tren_data
                elif tren_data["disponible"]:
                    existente["disponible"] = True

        trains_found = list(trenes_unicos.values())
        trains_found = sorted(trains_found, key=lambda x: x['salida'])

        return trains_found

    except Exception as e:
        logger.exception("Error parseando el DWR: %s", e)
        return []


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

        return parsear_dwr_renfe(texto_dwr, date_str)

    except Exception as e:
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