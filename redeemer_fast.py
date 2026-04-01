"""
Redeemer Fast: Redención de PINs usando AJAX directo desde dentro del browser.
Mantiene páginas persistentes cargadas en redeem.hype.games.
Todo el flujo (validate, verify, confirm) se ejecuta vía JavaScript dentro
de la página, manteniendo sesión, cookies y reCAPTCHA naturalmente.

Ventajas vs redeemer.py original:
- No crea contextos nuevos por PIN
- No navega por cada PIN
- reCAPTCHA ya cargado
- ~2-3s/PIN vs ~4-5s/PIN
"""
import re
import asyncio
import time
from datetime import datetime, timezone
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
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


# ─── JavaScript que ejecuta TODO el flujo AJAX desde dentro de la página ────

REDEEM_JS = """
async (args) => {
    const [pin, name, bornAt, gameId, nationality] = args;

    // Helper: hacer AJAX como lo hace jQuery en la página
    function ajaxPost(url, data, extraHeaders) {
        return new Promise((resolve, reject) => {
            $.ajax({
                url: url,
                type: 'POST',
                data: data,
                headers: extraHeaders || {},
                success: (result, status, xhr) => resolve({
                    ok: true,
                    status: xhr.status,
                    data: result,
                    isHtml: typeof result === 'string'
                }),
                error: (xhr) => resolve({
                    ok: false,
                    status: xhr.status,
                    data: xhr.responseText || '',
                    isHtml: true
                })
            });
        });
    }

    // Helper: obtener token captcha
    async function getCaptchaToken() {
        const sdk = ajaxConfig.captchaEnterpriseEnabled
            ? grecaptcha.enterprise : grecaptcha;
        return new Promise((resolve) => {
            sdk.ready(() => {
                sdk.execute(ajaxConfig.captchaPublicKey, { action: 'KEY_REDEEM' })
                    .then(resolve);
            });
        });
    }

    try {
        // ═══ PASO 1: Validar PIN ═══
        const pinInput = document.querySelector('#pininput');
        if (!pinInput) return { step: 'validate', error: 'PIN input no encontrado' };

        // Limpiar estado previo
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
        setter.call(pinInput, pin);
        pinInput.dispatchEvent(new Event('input', { bubbles: true }));

        const token1 = await getCaptchaToken();
        const formData = $('#validate-form').serialize();
        const validateData = formData + '&CaptchaToken=' + encodeURIComponent(token1) + '&origin=' + ajaxConfig.origin;

        const v = await ajaxPost(ajaxConfig.urlValidate, validateData, {
            PartnerIdentifier: ajaxConfig.partnerIdentifier || ''
        });

        if (!v.ok) {
            return { step: 'validate', error: 'HTTP ' + v.status, returnPin: true };
        }

        // Si es JSON con StatusCode = error
        if (!v.isHtml && v.data && v.data.StatusCode) {
            return { step: 'validate', error: v.data.Message || 'Error validando', returnPin: true };
        }

        // Inyectar HTML del formulario de redención
        if (v.isHtml && typeof v.data === 'string') {
            ajaxConfig.actionBeforeFillHtml();
            $(ajaxConfig.contentClassName).html(v.data);
            // Registrar el submit handler del redeem-form
            if (typeof loadConfirmSubmitJs === 'function') loadConfirmSubmitJs();
        }

        // Extraer nombre del producto
        let productName = '';
        const prodEl = document.querySelector('.product-header h2');
        if (prodEl) productName = prodEl.textContent.trim();

        // ═══ PASO 2: Llenar formulario ═══
        await new Promise(r => setTimeout(r, 200)); // Esperar render

        const gameInput = document.querySelector('#GameAccountId');
        if (!gameInput) return { step: 'fill', error: 'Campo GameAccountId no encontrado', returnPin: true, productName };

        function setVal(el, val) {
            setter.call(el, val);
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new Event('keyup', { bubbles: true }));
        }

        const nameEl = document.querySelector('#Name');
        if (nameEl) setVal(nameEl, name);

        const bornEl = document.querySelector('#BornAt');
        if (bornEl) { bornEl.focus(); setVal(bornEl, bornAt); }

        setVal(gameInput, gameId);

        // Nacionalidad
        const natSelect = document.querySelector('#Customer\\\\.NationalityAlphaCode, [name="Customer.NationalityAlphaCode"]');
        if (natSelect) {
            // Esperar a que se carguen las opciones (getNationality() hace un getJSON)
            for (let i = 0; i < 20; i++) {
                if (natSelect.options.length > 1) break;
                await new Promise(r => setTimeout(r, 200));
            }
            natSelect.value = nationality;
            natSelect.dispatchEvent(new Event('change', { bubbles: true }));
        }

        // Privacy checkbox
        const privacy = document.querySelector('#privacy');
        if (privacy && !privacy.checked) {
            privacy.checked = true;
            privacy.dispatchEvent(new Event('change', { bubbles: true }));
        }

        // Cookie button
        const cookieBtn = document.querySelector('#adopt-accept-all-button');
        if (cookieBtn) cookieBtn.click();

        // ═══ PASO 3: Verificar cuenta (AJAX) ═══
        const token2 = await getCaptchaToken();
        const redeemFormData = $('#redeem-form').serialize();
        const countryId = $('#CountryId').val() || '';
        const verifyData = redeemFormData
            + '&CaptchaToken=' + encodeURIComponent(token2)
            + '&origin=' + ajaxConfig.origin
            + '&CountryId=' + encodeURIComponent(countryId);

        let nickname = '';
        let verifyOk = false;

        for (let attempt = 0; attempt < 3; attempt++) {
            const vr = await ajaxPost('validate/account', verifyData, {
                PartnerIdentifier: ajaxConfig.partnerIdentifier || ''
            });

            if (vr.ok && vr.data && vr.data.Success === true) {
                nickname = vr.data.Username || '';
                verifyOk = true;
                // Mostrar nickname en la UI
                if (typeof setPlayerGameName === 'function') {
                    setPlayerGameName(nickname);
                }
                break;
            }

            const msg = (vr.data && vr.data.Message) ? vr.data.Message : 'Error verificando';
            if (msg.toLowerCase().includes('interno') || msg.toLowerCase().includes('internal')) {
                await new Promise(r => setTimeout(r, 500));
                continue;
            }

            return { step: 'verify', error: msg, returnPin: true, productName, nickname: '' };
        }

        if (!verifyOk) {
            return { step: 'verify', error: 'Error verificando ID tras 3 intentos', returnPin: true, productName };
        }

        // ═══ PASO 4: Canjear (confirm AJAX) ═══
        const token3 = await getCaptchaToken();
        const confirmFormData = $('#redeem-form').serialize();
        const confirmData = confirmFormData
            + '&CaptchaToken=' + encodeURIComponent(token3)
            + '&origin=' + ajaxConfig.origin;

        const cr = await ajaxPost(ajaxConfig.urlConfirm, confirmData, {
            PartnerIdentifier: ajaxConfig.partnerIdentifier || ''
        });

        // ═══ PASO 5: Evaluar resultado ═══
        if (cr.ok) {
            // Si es JSON con error
            if (!cr.isHtml && cr.data && cr.data.StatusCode) {
                return { step: 'confirm', error: cr.data.Message || 'Error', returnPin: false, productName, nickname };
            }

            // Inyectar HTML de resultado
            if (cr.isHtml && typeof cr.data === 'string') {
                ajaxConfig.actionBeforeFillHtml();
                $(ajaxConfig.contentClassName).html(cr.data);
            }

            return {
                step: 'done',
                success: true,
                productName: productName,
                nickname: nickname,
                error: ''
            };
        }

        return { step: 'confirm', error: 'HTTP ' + cr.status, returnPin: false, productName, nickname };

    } catch (e) {
        return { step: 'exception', error: e.message || String(e), returnPin: true };
    }
}
"""

# JavaScript para resetear la página al estado inicial (sin recargar)
RESET_JS = """
() => {
    // Volver a la vista del PIN input
    const cardContent = document.querySelector('.card-content');
    if (cardContent) {
        cardContent.classList.remove('fliped');
        const front = cardContent.querySelector('.card.front');
        if (front) front.classList.remove('d-none');
    }
    // Limpiar el PIN input
    const pinInput = document.querySelector('#pininput');
    if (pinInput) {
        const s = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
        s.call(pinInput, '');
        pinInput.dispatchEvent(new Event('input', { bubbles: true }));
    }
    // Limpiar errores
    const errorEls = document.querySelectorAll('.text-danger, .error-message, .alert-danger');
    errorEls.forEach(el => el.textContent = '');
    return true;
}
"""


# ─── Redeemer Fast ───────────────────────────────────────────────────────────

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
    "--metrics-recording-only",
]

BLOCKED_PATTERNS = [
    "clarity.ms", "google-analytics", "googletagmanager",
    "/Content/images/covers/", "/Content/favicon/",
    ".woff", ".woff2", ".ttf",
    "ubistatic2-a.akamaihd.net", "goadopt.io",
]

ALLOW_PATTERNS = [
    "recaptcha", "gstatic.com", "google.com/recaptcha", "hype.games",
]


class RedeemSlot:
    """Una página persistente lista para redimir PINs."""

    def __init__(self, index: int):
        self.index = index
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.lock = asyncio.Lock()
        self.ready = False
        self.use_count = 0

    async def setup(self, browser: Browser):
        """Crea contexto y navega a la página base."""
        self.context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="es-CL",
        )
        await self.context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['es-CL', 'es', 'en'] });
        """)

        async def block_resources(route):
            url = route.request.url
            if any(p in url for p in ALLOW_PATTERNS):
                await route.continue_()
                return
            if any(p in url for p in BLOCKED_PATTERNS):
                await route.abort()
                return
            rtype = route.request.resource_type
            if rtype in ("image", "font", "media"):
                await route.abort()
                return
            await route.continue_()

        await self.context.route("**/*", block_resources)
        self.page = await self.context.new_page()
        self.page.set_default_timeout(config.REDEEM_TIMEOUT * 1000)
        await self._navigate()

    async def _navigate(self):
        """Navega a la página y espera reCAPTCHA."""
        await self.page.goto(config.REDEEM_BASE_URL, wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(0.5)

        for _ in range(30):
            ok = await self.page.evaluate(
                "() => typeof grecaptcha !== 'undefined' && typeof grecaptcha.execute === 'function'"
            )
            if ok:
                break
            await asyncio.sleep(0.3)

        self.ready = True
        self.use_count = 0

    async def redeem(self, pin: str, game_account_id: str) -> RedeemResult:
        """Ejecuta el flujo completo de redención vía AJAX dentro de la página."""
        product_name = ""
        try:
            # Cada 10 usos, recargar para evitar memory leaks
            if self.use_count >= 10:
                logger.info(f"[Slot {self.index}] Recargando página (uso #{self.use_count})...")
                await self._navigate()

            self.use_count += 1

            logger.info(f"[{pin[:8]}...] Ejecutando flujo AJAX (slot {self.index})...")

            result = await self.page.evaluate(
                REDEEM_JS,
                [pin, config.REDEEM_NAME, config.REDEEM_BORN_AT,
                 game_account_id, config.REDEEM_NATIONALITY]
            )

            logger.info(f"[{pin[:8]}...] Resultado JS: {result}")

            step = result.get("step", "?")
            error = result.get("error", "")
            product_name = result.get("productName", "")
            nickname = result.get("nickname", "")
            return_pin = result.get("returnPin", True)

            if result.get("success"):
                diamonds = parse_diamonds(product_name)
                logger.success(f"[{pin[:8]}...] EXITOSO → {nickname} | {diamonds} diamantes | {product_name}")
                return RedeemResult(
                    success=True, pin=pin, product_name=product_name,
                    nickname=nickname, diamonds=diamonds,
                )

            # Clasificar error
            error_type = _classify_error(error, step)
            if step == "confirm":
                return_pin = False  # PIN posiblemente consumido

            return RedeemResult.fail(pin, error_type, f"[{step}] {error}",
                                     return_pin=return_pin, product_name=product_name)

        except Exception as e:
            error_str = str(e)
            logger.error(f"[{pin[:8]}...] Error en slot {self.index}: {error_str}")
            if "timeout" in error_str.lower():
                return RedeemResult.fail(pin, ErrorType.TIMEOUT, error_str, return_pin=True)
            return RedeemResult.fail(pin, ErrorType.PAGE_ERROR, error_str,
                                     return_pin=True, product_name=product_name)

        finally:
            # Después de cada PIN (éxito o error), recargar la página completa.
            # El reset visual (RESET_JS) no es suficiente porque el servidor
            # de Hype mantiene estado de sesión (PIN validado, etc.)
            # y un reset parcial puede dejar la sesión en estado inconsistente.
            try:
                await self._navigate()
            except Exception:
                self.ready = False

    async def close(self):
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass


def _classify_error(message: str, step: str) -> ErrorType:
    msg = message.lower()
    if any(w in msg for w in ["expirado", "expired", "vencido"]):
        return ErrorType.PIN_EXPIRED
    if any(w in msg for w in ["canjeado", "redeemed", "usado", "used"]):
        return ErrorType.PIN_ALREADY_USED
    if any(w in msg for w in ["inválido", "invalid"]) and step == "verify":
        return ErrorType.INVALID_ID
    if "timeout" in msg:
        return ErrorType.TIMEOUT
    if step == "validate":
        return ErrorType.PIN_EXPIRED
    return ErrorType.UNKNOWN


class HypeFastRedeemer:
    """
    Motor rápido de redención: páginas persistentes + AJAX directo.
    N slots = N redenciones simultáneas, sin crear contextos nuevos.
    """

    def __init__(self):
        self._playwright = None
        self._browser: Browser | None = None
        self._slots: list[RedeemSlot] = []
        self._semaphore = asyncio.Semaphore(config.MAX_CONCURRENT)
        self._slot_index = 0
        self._initialized = False

    async def initialize(self):
        if self._initialized:
            return
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=config.HEADLESS,
            args=BROWSER_ARGS,
        )

        # Pre-cargar TODOS los slots para que cada canje sea rápido (~3s)
        for i in range(config.MAX_CONCURRENT):
            slot = RedeemSlot(i)
            await slot.setup(self._browser)
            self._slots.append(slot)
            logger.info(f"Slot {i + 1}/{config.MAX_CONCURRENT} listo ✓")

        self._initialized = True
        logger.info(
            f"HypeFastRedeemer inicializado: {config.MAX_CONCURRENT} slots activos, "
            f"headless={config.HEADLESS}"
        )

    async def shutdown(self):
        for slot in self._slots:
            await slot.close()
        self._slots.clear()
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._playwright:
            await self._playwright.stop()
        self._initialized = False
        logger.info("HypeFastRedeemer cerrado")

    async def _get_slot(self) -> RedeemSlot:
        """Busca un slot libre o crea uno nuevo si hay capacidad."""
        # Buscar un slot que no esté en uso
        for slot in self._slots:
            if not slot.lock.locked():
                return slot

        # Todos ocupados: crear uno nuevo si no superamos MAX_CONCURRENT
        if len(self._slots) < config.MAX_CONCURRENT:
            idx = len(self._slots)
            slot = RedeemSlot(idx)
            await slot.setup(self._browser)
            self._slots.append(slot)
            logger.info(f"Slot {idx + 1} creado bajo demanda ✓ (total: {len(self._slots)})")
            return slot

        # Todos ocupados y al máximo: round-robin (esperará el lock)
        slot = self._slots[self._slot_index % len(self._slots)]
        self._slot_index += 1
        return slot

    async def redeem_pin(self, pin: str, game_account_id: str) -> RedeemResult:
        async with self._semaphore:
            slot = await self._get_slot()

            async with slot.lock:
                t0 = time.monotonic()

                # Si el slot no está listo, recargarlo
                if not slot.ready:
                    try:
                        await slot._navigate()
                    except Exception as e:
                        return RedeemResult.fail(pin, ErrorType.PAGE_ERROR,
                                                 f"Slot no disponible: {e}", return_pin=True)

                result = await slot.redeem(pin, game_account_id)
                result.redeem_duration_ms = int((time.monotonic() - t0) * 1000)
                return result


# Instancia global
redeemer = HypeFastRedeemer()
