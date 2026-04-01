"""
Redeemer HTTP: Redención de PINs vía HTTP directo + browser mínimo solo para captcha.
Elimina el overhead de Playwright completo por cada PIN.
~1-2s/PIN vs ~4-5s/PIN con browser completo.
"""
import re
import asyncio
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Browser, Page
from loguru import logger

import config
from models import ErrorType


# ─── Resultado ───────────────────────────────────────────────────────────────

class RedeemResult:
    def __init__(self, success: bool, pin: str, error: ErrorType = ErrorType.NONE,
                 error_message: str = "", product_name: str = "", nickname: str = "",
                 diamonds: int = 0, return_pin: bool = False, redeem_duration_ms: int = 0):
        self.success = success
        self.pin = pin
        self.error = error
        self.error_message = error_message
        self.product_name = product_name
        self.nickname = nickname
        self.diamonds = diamonds
        self.return_pin = return_pin
        self.redeem_duration_ms = redeem_duration_ms
        self.redeemed_at = datetime.now(timezone.utc).isoformat() if success else ""

    @staticmethod
    def fail(pin: str, error: ErrorType, message: str, return_pin: bool,
             product_name: str = "") -> "RedeemResult":
        return RedeemResult(
            success=False, pin=pin, error=error, error_message=message,
            return_pin=return_pin, product_name=product_name,
        )


DIAMOND_PATTERN = re.compile(r"(\d+)\s*(?:diamantes|diamonds)", re.IGNORECASE)


def parse_diamonds(product_name: str) -> int:
    match = DIAMOND_PATTERN.search(product_name)
    return int(match.group(1)) if match else 0


# ─── Proveedor de tokens reCAPTCHA ──────────────────────────────────────────

class CaptchaProvider:
    """
    Mantiene UN solo browser con N páginas cargadas en redeem.hype.games.
    Genera tokens reCAPTCHA v3 llamando grecaptcha.execute() sin navegar.
    """

    SITE_KEY = "6Lf_DWEpAAAAAEg4rjruIXopl29ai0v9o6Vafx0A"
    BASE_URL = "https://redeem.hype.games"

    BROWSER_ARGS = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-images",
        "--disable-extensions",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-translate",
        "--metrics-recording-only",
        "--no-first-run",
    ]

    def __init__(self, num_pages: int = 2):
        self._num_pages = num_pages
        self._playwright = None
        self._browser: Browser | None = None
        self._pages: list[Page] = []
        self._locks: list[asyncio.Lock] = []
        self._page_index = 0
        self._initialized = False

    async def initialize(self):
        if self._initialized:
            return
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=config.HEADLESS,
            args=self.BROWSER_ARGS,
        )

        for i in range(self._num_pages):
            ctx = await self._browser.new_context(
                viewport={"width": 800, "height": 600},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            )
            page = await ctx.new_page()

            # Bloquear todo excepto scripts esenciales (recaptcha + jQuery)
            async def block_heavy(route):
                url = route.request.url
                rtype = route.request.resource_type
                if rtype in ("image", "font", "media", "stylesheet"):
                    await route.abort()
                    return
                if any(p in url for p in ["clarity.ms", "goadopt.io", "google-analytics",
                                           "googletagmanager", "ubistatic2-a.akamaihd"]):
                    await route.abort()
                    return
                await route.continue_()

            await ctx.route("**/*", block_heavy)
            await page.goto(self.BASE_URL, wait_until="domcontentloaded", timeout=20000)

            # Esperar a que reCAPTCHA esté listo
            for _ in range(40):
                ready = await page.evaluate(
                    "() => typeof grecaptcha !== 'undefined' && typeof grecaptcha.execute === 'function'"
                )
                if ready:
                    break
                await asyncio.sleep(0.25)

            self._pages.append(page)
            self._locks.append(asyncio.Lock())
            logger.info(f"Captcha page {i + 1}/{self._num_pages} lista ✓")

        self._initialized = True
        logger.info(f"CaptchaProvider inicializado con {self._num_pages} páginas")

    async def get_token(self) -> str:
        """Genera un token reCAPTCHA v3 fresco usando round-robin entre páginas."""
        idx = self._page_index % self._num_pages
        self._page_index += 1

        async with self._locks[idx]:
            page = self._pages[idx]
            try:
                token = await page.evaluate(
                    f"() => grecaptcha.execute('{self.SITE_KEY}', {{action: 'KEY_REDEEM'}})"
                )
                return token
            except Exception as e:
                logger.warning(f"Error generando captcha token (page {idx}), recargando: {e}")
                await self._reload_page(idx)
                token = await page.evaluate(
                    f"() => grecaptcha.execute('{self.SITE_KEY}', {{action: 'KEY_REDEEM'}})"
                )
                return token

    async def _reload_page(self, idx: int):
        """Recarga una página de captcha si falla."""
        page = self._pages[idx]
        try:
            await page.goto(self.BASE_URL, wait_until="domcontentloaded", timeout=15000)
            for _ in range(30):
                ready = await page.evaluate(
                    "() => typeof grecaptcha !== 'undefined' && typeof grecaptcha.execute === 'function'"
                )
                if ready:
                    break
                await asyncio.sleep(0.3)
        except Exception as e:
            logger.error(f"Error recargando captcha page {idx}: {e}")

    async def shutdown(self):
        for page in self._pages:
            try:
                await page.context.close()
            except Exception:
                pass
        self._pages.clear()
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._playwright:
            await self._playwright.stop()
        self._initialized = False
        logger.info("CaptchaProvider cerrado")


# ─── Redeemer HTTP ───────────────────────────────────────────────────────────

class HypeHTTPRedeemer:
    """
    Motor de redención vía HTTP directo.
    Solo usa browser para generar tokens reCAPTCHA.
    """

    BASE_URL = "https://redeem.hype.games"

    COMMON_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
        "Origin": "https://redeem.hype.games",
        "Referer": "https://redeem.hype.games/",
    }

    AJAX_HEADERS = {
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "*/*",
    }

    def __init__(self):
        self._captcha = CaptchaProvider(num_pages=2)
        self._semaphore = asyncio.Semaphore(config.MAX_CONCURRENT)
        self._initialized = False

    async def initialize(self):
        if self._initialized:
            return
        await self._captcha.initialize()
        self._initialized = True
        logger.info(
            f"HypeHTTPRedeemer inicializado: "
            f"max_concurrent={config.MAX_CONCURRENT}, "
            f"headless={config.HEADLESS}"
        )

    async def shutdown(self):
        await self._captcha.shutdown()
        self._initialized = False
        logger.info("HypeHTTPRedeemer cerrado")

    async def redeem_pin(self, pin: str, game_account_id: str) -> RedeemResult:
        async with self._semaphore:
            t0 = time.monotonic()
            result = await self._do_redeem(pin, game_account_id)
            result.redeem_duration_ms = int((time.monotonic() - t0) * 1000)
            return result

    async def _do_redeem(self, pin: str, game_account_id: str) -> RedeemResult:
        nickname = ""
        product_name = ""
        redeem_sent = False

        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(30.0),
                headers=self.COMMON_HEADERS,
            ) as client:

                # ── PASO 0: Obtener cookies de sesión ──
                logger.info(f"[{pin[:8]}...] Obteniendo sesión...")
                session_resp = await client.get(self.BASE_URL + "/")
                if session_resp.status_code != 200:
                    return RedeemResult.fail(pin, ErrorType.PAGE_ERROR,
                                             f"Error obteniendo sesión: HTTP {session_resp.status_code}",
                                             return_pin=True)

                # ── PASO 1: Validar PIN ──
                logger.info(f"[{pin[:8]}...] Validando PIN...")
                captcha_token = await self._captcha.get_token()

                validate_data = urlencode({
                    "Key": pin,
                    "CaptchaToken": captcha_token,
                    "origin": "redeem",
                })

                validate_resp = await client.post(
                    self.BASE_URL + "/validate",
                    content=validate_data,
                    headers=self.AJAX_HEADERS,
                )

                if validate_resp.status_code >= 400:
                    return RedeemResult.fail(pin, ErrorType.PIN_EXPIRED,
                                             f"Error validando PIN: HTTP {validate_resp.status_code}",
                                             return_pin=True)

                # Verificar si es JSON de error
                resp_text = validate_resp.text
                try:
                    json_resp = validate_resp.json()
                    if json_resp.get("StatusCode"):
                        msg = json_resp.get("Message", "Error de validación")
                        error_type = self._classify_error(msg)
                        return_pin = error_type not in (ErrorType.PIN_EXPIRED, ErrorType.PIN_ALREADY_USED)
                        return RedeemResult.fail(pin, error_type, msg, return_pin=return_pin)
                except Exception:
                    pass  # Es HTML — PIN válido

                # ── PASO 2: Parsear HTML del formulario ──
                html = resp_text
                soup = BeautifulSoup(html, "html.parser")

                # Extraer nombre del producto
                product_el = soup.select_one(".product-header h2, .product-name, h2")
                if product_el:
                    product_name = product_el.get_text(strip=True)
                    logger.info(f"[{pin[:8]}...] Producto: {product_name}")

                # Extraer campos como lista de tuplas (preserva duplicados y orden)
                # Replica exactamente jQuery $("form").serialize()
                form_fields: list[tuple[str, str]] = []
                redeem_form = soup.select_one("#redeem-form") or soup.select_one("form")
                country_id_val = ""

                if redeem_form:
                    for el in redeem_form.select("input, select, textarea"):
                        name = el.get("name")
                        if not name:
                            continue
                        inp_type = el.get("type", "").lower()
                        # jQuery serialize: checkbox solo se incluye si está checked
                        if inp_type == "checkbox":
                            continue  # Lo marcamos nosotros abajo
                        if inp_type == "submit" or inp_type == "button":
                            continue
                        if el.name == "select":
                            selected = el.find("option", selected=True)
                            val = selected.get("value", "") if selected else ""
                        else:
                            val = el.get("value", "")
                        form_fields.append((name, val))
                        if name == "CountryId":
                            country_id_val = val

                logger.debug(f"[{pin[:8]}...] Campos del form: {[f[0] for f in form_fields]}")

                # Completar campos del formulario con datos fijos
                def set_field(fields, name, value):
                    """Establece el valor de un campo (reemplaza si existe, agrega si no)."""
                    for i, (n, _) in enumerate(fields):
                        if n == name:
                            fields[i] = (name, value)
                            return
                    fields.append((name, value))

                set_field(form_fields, "Name", config.REDEEM_NAME)
                set_field(form_fields, "BornAt", config.REDEEM_BORN_AT)
                set_field(form_fields, "GameAccountId", game_account_id)
                set_field(form_fields, "Customer.NationalityAlphaCode", config.REDEEM_NATIONALITY)
                # Checkbox privacy: jQuery envía "on" cuando está checked con value vacío
                form_fields.append(("privacy", "on"))

                # ── PASO 3: Verificar cuenta de juego ──
                # JS: data = $("form").serialize()
                #     + &CaptchaToken={token}&origin={origin}&CountryId={CountryId}
                logger.info(f"[{pin[:8]}...] Verificando ID: {game_account_id}")
                captcha_token2 = await self._captcha.get_token()

                verify_fields = list(form_fields)
                verify_fields.append(("CaptchaToken", captcha_token2))
                verify_fields.append(("origin", "redeem"))
                verify_fields.append(("CountryId", country_id_val))

                verify_ok = False
                for attempt in range(3):
                    try:
                        verify_resp = await client.post(
                            self.BASE_URL + "/validate/account",
                            content=urlencode(verify_fields),
                            headers=self.AJAX_HEADERS,
                        )
                        logger.debug(f"[{pin[:8]}...] Verify HTTP {verify_resp.status_code}")
                        resp_text_v = verify_resp.text

                        if verify_resp.status_code >= 500:
                            logger.warning(f"[{pin[:8]}...] Verify 500: {resp_text_v[:200]}")
                            if attempt < 2:
                                await asyncio.sleep(0.5)
                                continue
                            return RedeemResult.fail(pin, ErrorType.PAGE_ERROR,
                                                     f"Server error verificando ID: HTTP {verify_resp.status_code}",
                                                     return_pin=True, product_name=product_name)

                        verify_json = verify_resp.json()
                        logger.info(f"[{pin[:8]}...] Verify: {verify_json}")

                        if verify_json.get("Success"):
                            verify_ok = True
                            nickname = verify_json.get("Username", "")
                            break

                        error_msg = verify_json.get("Message", "Error verificando ID")
                        if "interno" in error_msg.lower() or "internal" in error_msg.lower():
                            logger.warning(f"[{pin[:8]}...] Error interno en verify, retry #{attempt + 1}")
                            await asyncio.sleep(0.5)
                            continue

                        return RedeemResult.fail(pin, ErrorType.INVALID_ID, error_msg,
                                                 return_pin=True, product_name=product_name)

                    except Exception as e:
                        if attempt == 2:
                            return RedeemResult.fail(pin, ErrorType.TIMEOUT,
                                                     f"Timeout verificando ID: {str(e)}",
                                                     return_pin=True, product_name=product_name)
                        await asyncio.sleep(0.5)
                        continue

                if not verify_ok:
                    return RedeemResult.fail(pin, ErrorType.INVALID_ID,
                                             "Error verificando ID tras 3 intentos",
                                             return_pin=True, product_name=product_name)

                logger.info(f"[{pin[:8]}...] Nickname: {nickname}")

                # ── PASO 4: Canjear (confirmar) ──
                # JS: data = $("form").serialize() + &CaptchaToken={token}&origin={origin}
                logger.info(f"[{pin[:8]}...] Canjeando...")
                captcha_token3 = await self._captcha.get_token()

                confirm_fields = list(form_fields)
                confirm_fields.append(("CaptchaToken", captcha_token3))
                confirm_fields.append(("origin", "redeem"))

                redeem_sent = True
                confirm_resp = await client.post(
                    self.BASE_URL + "/confirm",
                    content=urlencode(confirm_fields),
                    headers=self.AJAX_HEADERS,
                )

                # ── PASO 5: Verificar resultado ──
                if confirm_resp.status_code == 200:
                    confirm_text = confirm_resp.text

                    # Verificar si es JSON de error
                    try:
                        confirm_json = confirm_resp.json()
                        if confirm_json.get("StatusCode"):
                            msg = confirm_json.get("Message", "Error en confirmación")
                            return RedeemResult.fail(pin, ErrorType.UNKNOWN, msg,
                                                     return_pin=False, product_name=product_name)
                    except Exception:
                        pass  # Es HTML

                    # Buscar indicadores de éxito en el HTML
                    success_keywords = ["exitoso", "sucesso", "success", "entregado",
                                        "delivered", "créditos", "creditos", "diamantes",
                                        "completado", "realizado"]

                    if any(w in confirm_text.lower() for w in success_keywords):
                        diamonds = parse_diamonds(product_name)
                        logger.success(f"[{pin[:8]}...] EXITOSO → {nickname} | {diamonds} diamantes")
                        return RedeemResult(
                            success=True, pin=pin, product_name=product_name,
                            nickname=nickname, diamonds=diamonds,
                        )

                    # Si no hay keywords pero tampoco es error JSON, asumir éxito
                    # (el /confirm devuelve HTML de éxito que se inyecta en la página)
                    diamonds = parse_diamonds(product_name)
                    logger.success(f"[{pin[:8]}...] EXITOSO (HTTP 200) → {nickname} | {diamonds} diamantes")
                    return RedeemResult(
                        success=True, pin=pin, product_name=product_name,
                        nickname=nickname, diamonds=diamonds,
                    )

                # Error HTTP en confirm
                return RedeemResult.fail(pin, ErrorType.UNKNOWN,
                                         f"Error en confirm: HTTP {confirm_resp.status_code}",
                                         return_pin=False, product_name=product_name)

        except Exception as e:
            error_str = str(e)
            logger.error(f"[{pin[:8]}...] Error: {error_str}")
            if redeem_sent:
                return RedeemResult.fail(pin, ErrorType.UNKNOWN, error_str,
                                         return_pin=False, product_name=product_name)
            if "timeout" in error_str.lower():
                return RedeemResult.fail(pin, ErrorType.TIMEOUT, error_str, return_pin=True)
            return RedeemResult.fail(pin, ErrorType.PAGE_ERROR, error_str, return_pin=True)

    @staticmethod
    def _classify_error(message: str) -> ErrorType:
        """Clasifica el tipo de error basado en el mensaje."""
        msg = message.lower()
        if any(w in msg for w in ["expirado", "expired", "vencido"]):
            return ErrorType.PIN_EXPIRED
        if any(w in msg for w in ["canjeado", "redeemed", "usado", "used"]):
            return ErrorType.PIN_ALREADY_USED
        if any(w in msg for w in ["inválido", "invalid", "no encontrado", "not found"]):
            return ErrorType.PIN_EXPIRED
        if any(w in msg for w in ["interno", "internal"]):
            return ErrorType.PAGE_ERROR
        return ErrorType.UNKNOWN


# Instancia global del redeemer
redeemer = HypeHTTPRedeemer()
