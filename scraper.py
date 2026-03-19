import asyncio
from playwright.async_api import async_playwright
import re

async def get_trains(origin: str, destination: str, date_str: str):
    """
    Navega a Renfe y extrae los trenes disponibles para una fecha y ruta.
    date_str debe tener el formato DD/MM/AAAA.
    """
    trains_found = []

    async with async_playwright() as p:
        # Lanzamos el navegador (headless=True para que no abra ventana visual)
        browser = await p.chromium.launch(headless=True)
        # Usamos un user-agent real para evitar bloqueos básicos
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        try:
            print(f"Buscando trenes: {origin} -> {destination} el {date_str}...")
            await page.goto("https://www.renfe.com/es/es", timeout=60000)

            # 1. Aceptar cookies
            try:
                await page.click("button#onetrust-accept-btn-handler", timeout=5000)
                await page.wait_for_timeout(500)
            except:
                pass 

            # 2. Rellenar y SELECCIONAR Origen (Modo Teclado)
            print(f"Escribiendo ORIGEN: {origin}")
            await page.click("input#origin")
            await page.wait_for_timeout(200)
            # Borramos por si había algo escrito
            await page.fill("input#origin", "") 
            await page.locator("input#origin").press_sequentially(origin, delay=150)
            
            # Damos 2 segundos para que Renfe piense y despliegue la lista
            await page.wait_for_timeout(2000) 
            
            # Pulsamos Flecha Abajo para marcar el primer resultado y Enter para elegirlo
            await page.keyboard.press("ArrowDown")
            await page.wait_for_timeout(200)
            await page.keyboard.press("Enter")
            print("ORIGEN seleccionado con éxito.")
            
            await page.wait_for_timeout(800)

            # 3. Rellenar y SELECCIONAR Destino (Modo Teclado)
            print(f"Escribiendo DESTINO: {destination}")
            await page.click("input#destination")
            await page.wait_for_timeout(200)
            await page.fill("input#destination", "")
            await page.locator("input#destination").press_sequentially(destination, delay=150)
            
            await page.wait_for_timeout(2000)
            
            await page.keyboard.press("ArrowDown")
            await page.wait_for_timeout(200)
            await page.keyboard.press("Enter")
            print("DESTINO seleccionado con éxito.")

            await page.wait_for_timeout(500)

            # 4. Forzar "Viaje solo ida" para evitar comportamientos inconsistentes
            try:
                await page.click("label[for='trip-go']", timeout=5000)
                print("Marcado solo ida")
            except:
                # Fallback robusto: muchos radios están ocultos, lo marcamos por JS.
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

            # 5. Rellenar fecha de ida actualizando también campos internos/ocultos
            fecha_asignada = await page.evaluate(
                """
                (value) => {
                    const m = /^(\d{2})\/(\d{2})\/(\d{4})$/.exec(value || '');
                    if (!m) return { ok: false, reason: 'Formato de fecha inválido' };

                    const dd = m[1];
                    const mm = m[2];
                    const yyyy = m[3];
                    const iso = `${yyyy}-${mm}-${dd}`;
                    const compact = `${yyyy}${mm}${dd}`;

                    const containsAny = (txt, keys) => keys.some((k) => txt.includes(k));

                    const isDepartureField = (el) => {
                        const bag = [
                            el.id || '',
                            el.name || '',
                            el.className || '',
                            el.getAttribute('aria-label') || '',
                            el.getAttribute('placeholder') || '',
                            el.getAttribute('title') || '',
                        ]
                            .join(' ')
                            .toLowerCase();

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

                            // Algunos formularios leen también formatos alternativos
                            if (input.dataset) {
                                input.dataset.date = value;
                                input.dataset.isoDate = iso;
                                input.dataset.compactDate = compact;
                            }

                            input.dispatchEvent(new Event('input', { bubbles: true }));
                            input.dispatchEvent(new Event('change', { bubbles: true }));
                            input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', bubbles: true }));
                            input.dispatchEvent(new Event('blur', { bubbles: true }));
                            updated += 1;
                        } catch (_) {
                            // Ignoramos inputs que no puedan editarse
                        }
                    }

                    // También sincronizamos posibles hidden de ida
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

            # 6. Click en Buscar billete
            print("Haciendo clic en 'Buscar billete'...")
            search_button = "button[title='Buscar billete']"
            await page.wait_for_selector(search_button, state="visible", timeout=10000)
            await page.click(search_button)

            # 7. Esperar a que cargue la tabla de resultados
            # Renfe puede usar selectedTren o selectedTrain según versión
            await page.wait_for_selector("div.selectedTren, div.selectedTrain", timeout=25000)
            print("Tabla de trenes cargada con éxito.")
            
            # 8. Extraer la información ignorando resultados ocultos (display:none)
            rows = page.locator("div.selectedTren, div.selectedTrain")
            rows_count = await rows.count()
            trenes_unicos = {}

            # Regex para buscar el formato HH:MM
            patron_hora = re.compile(r'\d{2}:\d{2}')

            for i in range(rows_count):
                row = rows.nth(i)

                # Ignoramos filas ocultas (display:none o invisibles)
                if not await row.is_visible():
                    continue

                # --- 1. EXTRAER HORAS (div.trenes) ---
                div_horas = row.locator("div.trenes")
                if await div_horas.count() == 0:
                    continue
                    
                texto_horas = await div_horas.first.inner_text()
                horas_encontradas = patron_hora.findall(texto_horas)
                
                if len(horas_encontradas) >= 2:
                    salida = horas_encontradas[0]
                    llegada = horas_encontradas[-1]
                else:
                    continue

                # --- 2. EXTRAER ESTADO basándonos en el bloque de precio ---
                # Disponible: existe span.precio-final
                # Completo: aparece botón/mensaje de "Tren Completo"
                div_precio = row.locator("div[id^='precio-viaje']")
                is_full = True  # Fallback conservador

                if await div_precio.count() > 0:
                    bloque_precio = div_precio.first

                    tiene_precio = await bloque_precio.locator("span.precio-final").count() > 0
                    if tiene_precio:
                        is_full = False

                    tiene_boton_completo = await bloque_precio.locator(
                        "button:has-text('Tren Completo'), [title*='Tren Completo'], [title*='Completo']"
                    ).count() > 0
                    if tiene_boton_completo:
                        is_full = True

                    if not tiene_precio and not tiene_boton_completo:
                        texto_bruto = await bloque_precio.inner_text()
                        texto_limpio = re.sub(r'\s+', ' ', texto_bruto if texto_bruto else "").upper()
                        if "COMPLETO" in texto_limpio or "SOLO PLAZA H" in texto_limpio or "NO DISPONIBLE" in texto_limpio:
                            is_full = True
                        elif "€" in texto_limpio:
                            is_full = False

                # --- 3. LÓGICA ANTI-DUPLICADOS ---
                # Renfe pone varias filas iguales si hay diferentes tarifas (Básico, Elige...)
                if salida not in trenes_unicos:
                    trenes_unicos[salida] = {
                        "salida": salida,
                        "llegada": llegada,
                        "disponible": not is_full
                    }
                else:
                    # Si ya habíamos guardado este tren a esta hora y estaba lleno, 
                    # pero esta nueva tarifa SÍ tiene billete, lo actualizamos a Disponible.
                    if not is_full:
                        trenes_unicos[salida]["disponible"] = True

            # Convertimos nuestro diccionario a la lista que espera Telegram y la ordenamos por hora
            trains_found = list(trenes_unicos.values())
            trains_found = sorted(trains_found, key=lambda x: x['salida'])

        except Exception as e:
            print(f"Error durante el scraping: {e}")
            await page.screenshot(path="error_renfe.png", full_page=True)
            print("Captura de pantalla de error guardada en la raíz del proyecto.")
        finally:
            await browser.close()
            
    return trains_found