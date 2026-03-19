import codecs
import logging
import re
from typing import Any, Dict, List, Optional

import httpx
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)
API_TOKENS: Dict[str, Dict[str, Any]] = {}
ALLOWED_RESOURCE_TYPES = ["document", "script", "xhr", "fetch"]


def _build_search_key(origin: str, destination: str, date_str: str) -> str:
    return f"{origin}-{destination}-{date_str}"


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
    Aplica la triple regla de disponibilidad: tarifas nulas, solo plazas H, o bloqueo tipo 3.
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
            
            tarifas_m = re.search(r'tarifasDisponibles:\s*(null|\[)', bloque)
            solo_plazah_m = re.search(r'soloPlazaH:\s*(true|false)', bloque)
            razon_m = re.search(r'razonNoDisponible:\s*(null|"[^"]*")', bloque)
            
            if salida_m and llegada_m:
                salida = salida_m.group(1)
                llegada = llegada_m.group(1)

                origen_real = _decode_escaped_text(origen_m.group(1)) if origen_m else ""
                destino_real = _decode_escaped_text(destino_m.group(1)) if destino_m else ""
                
                is_full = False 
                
                if tarifas_m and tarifas_m.group(1) == 'null':
                    is_full = True
                    
                if solo_plazah_m and solo_plazah_m.group(1) == 'true':
                    is_full = True
                    
                if razon_m:
                    razon = razon_m.group(1)
                    if razon == '"3"':
                        is_full = True
                    
                if salida not in trenes_unicos:
                    trenes_unicos[salida] = {
                        "salida": salida,
                        "llegada": llegada,
                        "origen": origen_real.title(),
                        "destino": destino_real.title(),
                        "disponible": not is_full
                    }
                else:
                    if not is_full:
                        trenes_unicos[salida]["disponible"] = True

        trains_found = list(trenes_unicos.values())
        trains_found = sorted(trains_found, key=lambda x: x['salida'])
        
        return trains_found

    except Exception as e:
        logger.exception("Error parseando el DWR: %s", e)
        return []


async def _fetch_with_cached_session(search_key: str, date_str: str) -> Optional[List[Dict[str, Any]]]:
    session = API_TOKENS[search_key]
    payload = session["post_data"]
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


async def _capture_session_with_playwright(
    origin: str,
    destination: str,
    date_str: str,
    search_key: str,
) -> List[Dict[str, Any]]:
    logger.info("Iniciando Playwright para capturar sesion de Renfe")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
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
                await page.wait_for_timeout(500)
            except Exception:
                pass

            await page.click("input#origin")
            await page.wait_for_timeout(200)
            await page.fill("input#origin", "") 
            await page.locator("input#origin").press_sequentially(origin, delay=150)
            await page.wait_for_timeout(2000) 
            await page.keyboard.press("ArrowDown")
            await page.wait_for_timeout(200)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(800)

            await page.click("input#destination")
            await page.wait_for_timeout(200)
            await page.fill("input#destination", "")
            await page.locator("input#destination").press_sequentially(destination, delay=150)
            await page.wait_for_timeout(2000)
            await page.keyboard.press("ArrowDown")
            await page.wait_for_timeout(200)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(500)

            try:
                await page.click("label[for='trip-go']", timeout=5000)
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
            await page.wait_for_timeout(300)

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
            API_TOKENS[search_key] = {
                "url": api_request.url,
                "method": api_request.method,
                "headers": await api_request.all_headers(),
                "post_data": api_request.post_data
            }
            logger.info("Sesion y tokens de Renfe cacheados con exito")

            return parsear_dwr_renfe(texto_dwr, date_str)

        except Exception as e:
            logger.exception("Error durante captura de sesion DWR: %s", e)
            await page.screenshot(path="error_renfe.png", full_page=True)
            return []
            
        finally:
            if 'browser' in locals():
                await browser.close()


async def get_trains(origin: str, destination: str, date_str: str) -> List[Dict[str, Any]]:
    search_key = _build_search_key(origin, destination, date_str)

    if search_key in API_TOKENS:
        logger.info("Sesion activa para %s. Intentando API directa", search_key)
        try:
            cached_result = await _fetch_with_cached_session(search_key, date_str)
            if cached_result is not None:
                return cached_result
            del API_TOKENS[search_key]
        except Exception as e:
            logger.exception("Error en llamada API directa: %s", e)
            if search_key in API_TOKENS:
                del API_TOKENS[search_key]

    return await _capture_session_with_playwright(origin, destination, date_str, search_key)