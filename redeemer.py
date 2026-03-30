import re
import asyncio
from typing import Optional  # kept for future use
from datetime import datetime, timezone
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Route
from loguru import logger
import config
from models import ErrorType


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

# Patrones a bloquear para acelerar la carga
BLOCKED_PATTERNS = [
    "clarity.ms",
    "google-analytics",
    "googletagmanager",
    "/Content/images/covers/",
    "/Content/favicon/",
    ".woff", ".woff2", ".ttf",
    "ubistatic2-a.akamaihd.net",
    "goadopt.io",
]

# Nunca bloquear estos (reCAPTCHA + jQuery + scripts del sitio)
ALLOW_PATTERNS = [
    "recaptcha",
    "gstatic.com",
    "google.com/recaptcha",
    "hype.games",
]


def parse_diamonds(product_name: str) -> int:
    match = DIAMOND_PATTERN.search(product_name)
    if match:
        return int(match.group(1))
    return 0


class HypeRedeemer:
    """Motor de redención de PINs de Hype Games usando Playwright."""

    BROWSER_ARGS = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--disable-images",
        "--disable-extensions",
        "--disable-default-apps",
        "--no-first-run",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-translate",
        "--disable-component-update",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--metrics-recording-only",
        "--no-default-browser-check",
    ]

    def __init__(self):
        self._playwright = None
        self._browser: Browser | None = None
        self._semaphore = asyncio.Semaphore(config.MAX_CONCURRENT)
        self._initialized = False

    async def _launch_browser(self) -> Browser:
        """Lanza una instancia de Chromium."""
        return await self._playwright.chromium.launch(
            headless=config.HEADLESS,
            args=self.BROWSER_ARGS,
        )

    async def _ensure_browser(self):
        """Garantiza que el browser esté vivo. Lo reinicia si crasheó."""
        try:
            if self._browser and self._browser.is_connected():
                return
        except Exception:
            pass
        logger.warning("Browser caído, reiniciando...")
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        self._playwright = await async_playwright().start()
        self._browser = await self._launch_browser()
        logger.info("Browser reiniciado ✓")

    async def initialize(self):
        """Inicializa el navegador (sin pre-calentar páginas)."""
        if self._initialized:
            return
        self._playwright = await async_playwright().start()
        self._browser = await self._launch_browser()
        self._initialized = True

        logger.info(
            f"Inicializado: 1 browser, "
            f"max_concurrent={config.MAX_CONCURRENT}, "
            f"headless={config.HEADLESS}"
        )

    async def shutdown(self):
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        self._browser = None
        if self._playwright:
            await self._playwright.stop()
        self._initialized = False
        logger.info("Navegador cerrado")

    async def _make_context(self) -> BrowserContext:
        """Crea un contexto optimizado con bloqueo de recursos."""
        await self._ensure_browser()
        b = self._browser
        context = await b.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="es-CL",
        )
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['es-CL', 'es', 'en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        """)

        # Bloquear recursos innecesarios agresivamente
        async def block_resources(route: Route):
            url = route.request.url
            if any(p in url for p in ALLOW_PATTERNS):
                await route.continue_()
                return
            if any(p in url for p in BLOCKED_PATTERNS):
                await route.abort()
                return
            resource_type = route.request.resource_type
            if resource_type in ("image", "font", "media"):
                await route.abort()
                return
            await route.continue_()

        await context.route("**/*", block_resources)
        return context

    async def redeem_pin(self, pin: str, game_account_id: str) -> RedeemResult:
        async with self._semaphore:
            import time as _time
            t0 = _time.monotonic()
            result = await self._do_redeem(pin, game_account_id)
            result.redeem_duration_ms = int((_time.monotonic() - t0) * 1000)
            return result

    async def _do_redeem(self, pin: str, game_account_id: str) -> RedeemResult:
        context = None
        page = None
        nickname = ""
        product_name = ""

        redeem_clicked = False  # Flag: True after redeem button clicked (PIN possibly consumed)
        try:
            context = await self._make_context()
            page = await context.new_page()
            page.set_default_timeout(config.REDEEM_TIMEOUT * 1000)
            logger.info(f"[{pin[:8]}...] Navegando a página base...")
            await page.goto(config.REDEEM_BASE_URL, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(0.5)  # Cloudflare Rocket Loader

            # Esperar a que reCAPTCHA esté disponible
            recaptcha_ready = False
            for _ in range(20):
                recaptcha_ready = await page.evaluate(
                    "() => typeof window.grecaptcha !== 'undefined' && typeof window.grecaptcha.execute === 'function'"
                )
                if recaptcha_ready:
                    break
                await asyncio.sleep(0.3)
            logger.info(f"[{pin[:8]}...] reCAPTCHA listo: {recaptcha_ready}")

            # --- PASO 1: Ingresar PIN en #pininput + click Validar (AJAX, sin navegación) ---
            pin_input = page.locator("#pininput")
            try:
                await pin_input.wait_for(state="visible", timeout=5000)
            except Exception:
                # Si el input no aparece, la página no cargó bien — renavegar
                await page.goto(config.REDEEM_BASE_URL, wait_until="domcontentloaded", timeout=10000)
                await pin_input.wait_for(state="visible", timeout=5000)

            await pin_input.fill(pin)

            # Esperar a que btn-validate se habilite naturalmente (reCAPTCHA lo activa)
            btn_validate = page.locator("#btn-validate")
            await btn_validate.wait_for(state="visible", timeout=10000)
            for _ in range(30):
                disabled = await btn_validate.get_attribute("disabled")
                if disabled is None:
                    break
                await asyncio.sleep(0.15)
            else:
                # Fallback: forzar habilitación si reCAPTCHA no lo hizo
                await page.evaluate("document.querySelector('#btn-validate')?.removeAttribute('disabled')")

            # Click Validar e interceptar respuesta /validate
            try:
                async with page.expect_response(
                    lambda r: "/validate" in r.url and "account" not in r.url,
                    timeout=15000
                ) as resp_info:
                    await page.click("#btn-validate")
                validate_resp = await resp_info.value
                if validate_resp.status >= 400:
                    body = await validate_resp.text()
                    return RedeemResult.fail(pin, ErrorType.PIN_EXPIRED,
                                             f"Error validando PIN: HTTP {validate_resp.status}",
                                             return_pin=True)
            except Exception:
                pass  # Continuar y esperar card flip

            # Esperar card flip (formulario aparece)
            try:
                await page.wait_for_selector(".card.back .body", state="visible", timeout=15000)
            except Exception:
                error_el = await page.query_selector(".text-danger, .error-message, .alert-danger")
                if error_el:
                    error_text = (await error_el.text_content()).strip()
                    if any(w in error_text.lower() for w in ["expirado", "expired", "vencido"]):
                        return RedeemResult.fail(pin, ErrorType.PIN_EXPIRED, error_text, return_pin=False)
                    if any(w in error_text.lower() for w in ["canjeado", "redeemed", "usado", "used"]):
                        return RedeemResult.fail(pin, ErrorType.PIN_ALREADY_USED, error_text, return_pin=False)
                    return RedeemResult.fail(pin, ErrorType.PIN_EXPIRED, error_text, return_pin=False)
                return RedeemResult.fail(pin, ErrorType.TIMEOUT, "Timeout esperando validación del PIN", return_pin=True)

            # Obtener nombre del producto
            try:
                product_el = await page.query_selector(".product-header h2")
                if product_el:
                    product_name = (await product_el.text_content()).strip()
                    logger.info(f"[{pin[:8]}...] Producto: {product_name}")
            except Exception:
                pass

            # --- PASO 2: Llenar TODO de golpe con un solo evaluate ---
            # Esperar a que el campo GameAccountId esté visible
            try:
                await page.wait_for_selector("#GameAccountId", state="visible", timeout=5000)
            except Exception:
                pass
            logger.info(f"[{pin[:8]}...] Llenando formulario...")

            fill_ok = await page.evaluate("""(args) => {
                const [name, bornAt, gameId] = args;

                const cookieBtn = document.querySelector('#adopt-accept-all-button');
                if (cookieBtn) cookieBtn.click();

                const gameInput = document.querySelector('#GameAccountId');
                if (!gameInput || gameInput.offsetParent === null) return 'NO_GAME_FIELD';

                function setVal(el, val) {
                    const s = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                    s.call(el, val);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('keyup', { bubbles: true }));
                }

                const nameEl = document.querySelector('#Name');
                if (nameEl) setVal(nameEl, name);

                const bornEl = document.querySelector('#BornAt');
                if (bornEl) { bornEl.focus(); setVal(bornEl, bornAt); }

                setVal(gameInput, gameId);

                const privacy = document.querySelector('#privacy');
                if (privacy && !privacy.checked) { privacy.checked = true; privacy.dispatchEvent(new Event('change', { bubbles: true })); }

                const verifyBtn = document.querySelector('#btn-verify');
                if (verifyBtn) verifyBtn.removeAttribute('disabled');

                return 'OK';
            }""", [config.REDEEM_NAME, config.REDEEM_BORN_AT, game_account_id])

            if fill_ok == 'NO_GAME_FIELD':
                return RedeemResult.fail(pin, ErrorType.PAGE_ERROR,
                                         "Campo GameAccountId no encontrado",
                                         return_pin=True, product_name=product_name)

            # Nacionalidad — usar select_option de Playwright (maneja el dropdown nativo)
            await page.select_option("#NationalityAlphaCode", value=config.REDEEM_NATIONALITY)

            # --- PASO 3: Verificar ID (con delay + retry) ---
            await asyncio.sleep(0.3)  # Dar tiempo a Hype para estabilizarse
            logger.info(f"[{pin[:8]}...] Verificando ID: {game_account_id}")

            verify_ok = False
            for attempt in range(3):
                try:
                    if attempt > 0:
                        # Re-habilitar botón y esperar antes de reintentar
                        await page.evaluate("document.querySelector('#btn-verify')?.removeAttribute('disabled')")
                        await asyncio.sleep(0.5)
                        logger.info(f"[{pin[:8]}...] Retry verify #{attempt + 1}")

                    async with page.expect_response(
                        lambda r: "validate/account" in r.url, timeout=30000
                    ) as response_info:
                        await page.click("#btn-verify")

                    response = await response_info.value
                    response_data = await response.json()
                    logger.info(f"[{pin[:8]}...] Verify: {response_data}")

                    if response_data.get("Success"):
                        verify_ok = True
                        nickname = response_data.get("Username", "")
                        break

                    error_msg = response_data.get("Message", "Error verificando ID")
                    # Si es error interno, reintentar
                    if "interno" in error_msg.lower() or "internal" in error_msg.lower():
                        logger.warning(f"[{pin[:8]}...] Error interno en verify, reintentando...")
                        continue
                    # Error definitivo (ID inválido, etc)
                    return RedeemResult.fail(pin, ErrorType.INVALID_ID, error_msg,
                                             return_pin=True, product_name=product_name)

                except Exception as e:
                    if attempt == 2:
                        return RedeemResult.fail(pin, ErrorType.TIMEOUT,
                                                 f"Timeout verificando ID: {str(e)}",
                                                 return_pin=True, product_name=product_name)
                    continue

            if not verify_ok:
                return RedeemResult.fail(pin, ErrorType.INVALID_ID,
                                         "Error verificando ID tras 3 intentos",
                                         return_pin=True, product_name=product_name)

            logger.info(f"[{pin[:8]}...] Nickname: {nickname}")

            # --- PASO 4: Canjear ---
            logger.info(f"[{pin[:8]}...] Canjeando...")

            try:
                await page.wait_for_selector("#btn-redeem", state="visible", timeout=5000)
            except Exception:
                return RedeemResult.fail(pin, ErrorType.PAGE_ERROR,
                                         "Botón Canjear no apareció",
                                         return_pin=True, product_name=product_name)

            await page.evaluate("document.querySelector('#btn-redeem')?.removeAttribute('disabled')")

            try:
                async with page.expect_response(
                    lambda r: "confirm" in r.url, timeout=30000
                ) as response_info:
                    await page.click("#btn-redeem")
                    redeem_clicked = True  # PIN potentially consumed from this point
                confirm_resp = await response_info.value
                confirm_status = confirm_resp.status
            except Exception:
                redeem_clicked = True  # Click may have been sent
                confirm_status = -1

            # --- PASO 5: Verificar resultado directamente del response ---
            if confirm_status == 200:
                diamonds = parse_diamonds(product_name)
                logger.success(f"[{pin[:8]}...] EXITOSO -> {nickname} | {diamonds} diamantes")
                return RedeemResult(
                    success=True, pin=pin, product_name=product_name,
                    nickname=nickname, diamonds=diamonds,
                )

            # Fallback: leer DOM si el status no fue 200
            try:
                await page.wait_for_timeout(300)
                page_text = await page.evaluate("document.body.innerText")

                success_keywords = ["exitoso", "sucesso", "success", "entregado",
                                    "delivered", "créditos", "creditos", "diamantes",
                                    "completado", "realizado"]

                if any(w in page_text.lower() for w in success_keywords):
                    diamonds = parse_diamonds(product_name)
                    logger.success(f"[{pin[:8]}...] EXITOSO (DOM) -> {nickname} | {diamonds} diamantes")
                    return RedeemResult(
                        success=True, pin=pin, product_name=product_name,
                        nickname=nickname, diamonds=diamonds,
                    )
            except Exception as dom_err:
                logger.warning(f"[{pin[:8]}...] Error leyendo DOM post-redeem: {dom_err}")

            try:
                await page.screenshot(path=f"debug_{pin[:8]}.png", full_page=True)
            except Exception:
                pass

            return RedeemResult.fail(pin, ErrorType.UNKNOWN,
                                     "No se pudo confirmar. PIN posiblemente consumido.",
                                     return_pin=False, product_name=product_name)

        except Exception as e:
            error_str = str(e)
            logger.error(f"[{pin[:8]}...] Error: {error_str}")
            if redeem_clicked:
                logger.warning(f"[{pin[:8]}...] Exception AFTER redeem click — assuming PIN consumed, return_pin=False")
                return RedeemResult.fail(pin, ErrorType.UNKNOWN, error_str,
                                         return_pin=False, product_name=product_name)
            if "timeout" in error_str.lower():
                return RedeemResult.fail(pin, ErrorType.TIMEOUT, error_str, return_pin=True)
            return RedeemResult.fail(pin, ErrorType.PAGE_ERROR, error_str, return_pin=True)

        finally:
            if context:
                try:
                    await context.close()
                except Exception:
                    pass


# Instancia global del redeemer
redeemer = HypeRedeemer()
