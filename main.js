'use strict';

require('dotenv').config();
const fastify = require('fastify')({ logger: true });
const { chromium } = require('playwright');
const crypto = require('crypto');

// ═══════════════════════════════════════════════════════════════════════════
// Config (compatible con el .env existente de HypeAuto Python)
// ═══════════════════════════════════════════════════════════════════════════

const CONFIG = {
    API_SECRET_KEY: process.env.API_SECRET_KEY || 'change-me-in-production',
    PORT: parseInt(process.env.PORT || '8001', 10),
    MAX_CONCURRENT: parseInt(process.env.MAX_CONCURRENT || '5', 10),
    WEBHOOK_URL: process.env.WEBHOOK_URL || '',
    HEADLESS: (process.env.HEADLESS || 'true').toLowerCase() === 'true',
    REDEEM_TIMEOUT: parseInt(process.env.REDEEM_TIMEOUT || '60', 10) * 1000,
    REDEEM_URL: 'https://redeem.hype.games',
    REDEEM_NAME: process.env.REDEEM_NAME || 'Juan Perez',
    REDEEM_BORN_AT: process.env.REDEEM_BORN_AT || '15/03/1995',
    REDEEM_NATIONALITY: process.env.REDEEM_NATIONALITY || 'CL',
};

// ═══════════════════════════════════════════════════════════════════════════
// Constants
// ═══════════════════════════════════════════════════════════════════════════

const PIN_ERROR_KEYWORDS = [
    'already been redeemed', 'already been used',
    'invalid pin', 'pin inválido', 'pin inv',
    'já foi utilizado', 'pin not found',
    'código inválido', 'invalid code',
    'pin ya fue', 'ya fue canjeado',
    'not valid', 'não é válido',
    'já foi resgatado', 'expirado', 'expired',
];

const SUCCESS_KEYWORDS = [
    'successfully redeemed', 'canjeado con éxito',
    'resgatado com sucesso', 'congratulations',
    'canjeo exitoso', 'fue canjeado',
    'parabéns', 'felicidades',
    'your order has been', 'pedido foi',
];

const CONFIRM_ERROR_KEYWORDS = [
    'error', 'erro', 'failed', 'invalid', 'expired',
    'falhou', 'falló', 'tente novamente', 'try again',
];

const STILL_ON_FORM_KEYWORDS = [
    'editar dados', 'editar datos', 'edit data',
    'canjear ahora', 'resgatar agora', 'redeem now',
    'insira seu pin', 'ingrese su pin',
];

const BLOCKED_DOMAINS = [
    'google-analytics.com', 'googletagmanager.com',
    'facebook.net', 'facebook.com', 'fbcdn.net',
    'hotjar.com', 'doubleclick.net', 'googlesyndication.com',
    'cloudflareinsights.com', 'clarity.ms', 'goadopt.io',
];

const BROWSER_ARGS = [
    '--no-sandbox',
    '--disable-setuid-sandbox',
    '--disable-dev-shm-usage',
    '--disable-gpu',
    '--disable-software-rasterizer',
    '--disable-extensions',
    '--disable-background-networking',
    '--disable-default-apps',
    '--disable-sync',
    '--disable-translate',
    '--disable-component-update',
    '--no-first-run',
    '--disable-backgrounding-occluded-windows',
    '--disable-renderer-backgrounding',
    '--disable-ipc-flooding-protection',
    '--js-flags=--max-old-space-size=256',
];

const DIAMOND_RE = /(\d+)\s*(?:diamantes|diamonds)/i;

// ═══════════════════════════════════════════════════════════════════════════
// Error types (compatibles con jadhstore)
// ═══════════════════════════════════════════════════════════════════════════

const ErrorType = {
    NONE: '',
    INVALID_ID: 'invalid_id',
    PIN_EXPIRED: 'pin_expired',
    PIN_ALREADY_USED: 'pin_already_used',
    PAGE_ERROR: 'page_error',
    TIMEOUT: 'timeout',
    UNKNOWN: 'unknown',
};

function classifyError(step, message) {
    const msg = (message || '').toLowerCase();
    if (['expirado', 'expired', 'vencido'].some(w => msg.includes(w))) return ErrorType.PIN_EXPIRED;
    if (['canjeado', 'redeemed', 'usado', 'used', 'already'].some(w => msg.includes(w))) return ErrorType.PIN_ALREADY_USED;
    if (['inválido', 'invalid'].some(w => msg.includes(w)) && step === 'verify') return ErrorType.INVALID_ID;
    if (msg.includes('timeout')) return ErrorType.TIMEOUT;
    if (step === 'validate') return ErrorType.PIN_EXPIRED;
    return ErrorType.UNKNOWN;
}

function parseDiamonds(productName) {
    const m = DIAMOND_RE.exec(productName || '');
    return m ? parseInt(m[1], 10) : 0;
}

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

// ═══════════════════════════════════════════════════════════════════════════
// Semaphore
// ═══════════════════════════════════════════════════════════════════════════

class Semaphore {
    constructor(max) {
        this.max = max;
        this.current = 0;
        this.queue = [];
    }

    async acquire() {
        if (this.current < this.max) {
            this.current++;
            return this._createRelease();
        }
        await new Promise(resolve => this.queue.push(resolve));
        this.current++;
        return this._createRelease();
    }

    _createRelease() {
        let released = false;
        return () => {
            if (released) return;
            released = true;
            this.current--;
            const next = this.queue.shift();
            if (next) next();
        };
    }
}

const redeemSemaphore = new Semaphore(CONFIG.MAX_CONCURRENT);

// ═══════════════════════════════════════════════════════════════════════════
// Browser management
// ═══════════════════════════════════════════════════════════════════════════

let browser;
let browserLaunchPromise = null;

async function launchBrowser() {
    browser = await chromium.launch({
        headless: CONFIG.HEADLESS,
        args: BROWSER_ARGS,
    });
    fastify.log.info({ maxConcurrent: CONFIG.MAX_CONCURRENT }, 'Chromium listo');
    return browser;
}

async function closeBrowser() {
    if (!browser) return;
    try { await browser.close(); } catch {}
    browser = undefined;
}

function isBrowserReady() {
    try { return Boolean(browser && browser.isConnected()); } catch { return false; }
}

async function ensureBrowser() {
    if (isBrowserReady()) return browser;
    if (browserLaunchPromise) return browserLaunchPromise;

    browserLaunchPromise = (async () => {
        fastify.log.warn('Browser caído o no iniciado, relanzando...');
        await closeBrowser();
        return launchBrowser();
    })();

    try { return await browserLaunchPromise; }
    finally { browserLaunchPromise = null; }
}

// ═══════════════════════════════════════════════════════════════════════════
// Page Pool (pre-warm + recycle en background)
// ═══════════════════════════════════════════════════════════════════════════

const pagePool = [];
let poolFilling = false;

async function createWarmedPage() {
    const b = await ensureBrowser();
    const context = await b.newContext({
        viewport: { width: 1280, height: 800 },
        userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                 + '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        locale: 'es-CL',
    });

    // Block unnecessary resources
    await context.route('**/*', async (route) => {
        const url = route.request().url();
        const type = route.request().resourceType();

        if (url.includes('hype.games') || url.includes('recaptcha') || url.includes('gstatic.com')) {
            await route.continue();
            return;
        }

        if (['image', 'font', 'media', 'stylesheet'].includes(type) ||
            BLOCKED_DOMAINS.some(d => url.includes(d))) {
            await route.abort();
            return;
        }

        await route.continue();
    });

    const page = await context.newPage();
    page.setDefaultTimeout(CONFIG.REDEEM_TIMEOUT);

    await page.goto(CONFIG.REDEEM_URL, { waitUntil: 'domcontentloaded', timeout: 15000 });
    await page.waitForSelector('#pininput', { state: 'visible', timeout: 15000 });

    // Dismiss cookies
    await page.evaluate(() => {
        const btn = document.querySelector('#adopt-accept-all-button');
        if (btn) btn.click();
        document.querySelectorAll('[class*="cookie"], [class*="consent"], [id*="cookie"]')
            .forEach(el => el.remove());
    }).catch(() => {});

    // Wait for reCAPTCHA
    for (let i = 0; i < 30; i++) {
        const ready = await page.evaluate(
            () => typeof window.grecaptcha !== 'undefined' && typeof window.grecaptcha.execute === 'function'
        );
        if (ready) break;
        await sleep(150);
    }

    return { context, page, ready: true };
}

async function fillPool() {
    if (poolFilling) return;
    poolFilling = true;
    const target = CONFIG.MAX_CONCURRENT + 1;

    try {
        while (pagePool.length < target) {
            try {
                const entry = await createWarmedPage();
                pagePool.push(entry);
                fastify.log.info({ poolSize: pagePool.length, target }, 'Página pre-calentada');
            } catch (err) {
                fastify.log.warn({ err }, 'Error creando página para pool');
                break;
            }
        }
    } finally {
        poolFilling = false;
    }
}

async function acquirePage() {
    let entry = pagePool.shift();
    if (entry) {
        try {
            await entry.page.evaluate(() => true);
            return entry;
        } catch {
            try { await entry.context.close(); } catch {}
        }
    }
    // Pool empty — create on demand
    return createWarmedPage();
}

function recyclePage(entry) {
    // Fire-and-forget: recarga la página en background y la devuelve al pool
    (async () => {
        try {
            await entry.page.goto(CONFIG.REDEEM_URL, { waitUntil: 'domcontentloaded', timeout: 15000 });
            await entry.page.waitForSelector('#pininput', { state: 'visible', timeout: 15000 });

            await entry.page.evaluate(() => {
                document.querySelectorAll('[class*="cookie"], [class*="consent"]').forEach(el => el.remove());
            }).catch(() => {});

            for (let i = 0; i < 20; i++) {
                const ready = await entry.page.evaluate(
                    () => typeof window.grecaptcha !== 'undefined' && typeof window.grecaptcha.execute === 'function'
                );
                if (ready) break;
                await sleep(150);
            }

            if (pagePool.length < CONFIG.MAX_CONCURRENT + 1) {
                pagePool.push(entry);
                fastify.log.info({ poolSize: pagePool.length }, 'Página reciclada al pool');
            } else {
                await entry.context.close();
            }
        } catch (err) {
            fastify.log.warn({ err }, 'Error reciclando página, descartada');
            try { await entry.context.close(); } catch {}
            fillPool().catch(() => {});
        }
    })();
}

// ═══════════════════════════════════════════════════════════════════════════
// Redeem automation
// ═══════════════════════════════════════════════════════════════════════════

async function automateRedeem(pin, gameAccountId) {
    const startMs = Date.now();
    let entry;
    let shouldRecycle = false;
    let redeemClicked = false;

    try {
        entry = await acquirePage();
        const { page } = entry;

        fastify.log.info({ pin: pin.slice(0, 8), poolAcquireMs: Date.now() - startMs }, 'Página obtenida');

        // ─── PASO 1: Ingresar PIN + validar ───
        const stepLog = (step) => fastify.log.info({ pin: pin.slice(0, 8), step, ms: Date.now() - startMs }, 'step');
        stepLog('pin-input-wait');
        await page.waitForSelector('#pininput', { state: 'visible', timeout: 10000 });
        await page.evaluate((p) => {
            const el = document.querySelector('#pininput');
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
            setter.call(el, p);
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }, pin);

        stepLog('pin-filled');

        // Esperar a que reCAPTCHA habilite el botón
        stepLog('recaptcha-wait');
        await page.waitForFunction(
            () => {
                const btn = document.querySelector('#btn-validate');
                return btn && !btn.disabled;
            },
            { timeout: 15000, polling: 100 }
        );

        stepLog('btn-validate-ready');

        // Click validate + interceptar respuesta
        try {
            const validatePromise = page.waitForResponse(
                r => r.url().includes('/validate') && !r.url().includes('account'),
                { timeout: 15000 }
            );
            await page.evaluate(() => document.querySelector('#btn-validate').click());
            const validateResp = await validatePromise;

            if (validateResp.status() >= 400) {
                fastify.log.warn({ status: validateResp.status() }, '/validate HTTP error');
            }
        } catch (err) {
            fastify.log.warn({ err }, 'No se interceptó /validate');
        }

        stepLog('validate-clicked');

        // Esperar card flip
        try {
            await page.locator('.card.back').waitFor({ state: 'visible', timeout: 15000 });
        } catch {
            await sleep(500);
        }

        stepLog('card-flipped');

        // Verificar errores de PIN
        let pageText = await page.innerText('body');
        let lowerText = pageText.toLowerCase();

        const pinError = PIN_ERROR_KEYWORDS.find(kw => lowerText.includes(kw.toLowerCase()));
        if (pinError) {
            shouldRecycle = true;
            return {
                success: false,
                error: classifyError('validate', pinError),
                error_message: `Error de PIN: ${pinError}`,
                return_pin: true,
                nickname: '', product_name: '', diamonds: 0,
            };
        }

        // Extraer nombre del producto
        let productName = '';
        const prodEl = await page.$('.product-header h2');
        if (prodEl) productName = (await prodEl.textContent()).trim();

        // Verificar que el formulario apareció
        const cardBack = page.locator('.card.back');
        let cardBackHtml = '';
        if (await cardBack.count() > 0) {
            cardBackHtml = await cardBack.first().innerHTML();
        }
        if (!cardBackHtml || !cardBackHtml.includes('GameAccountId')) {
            shouldRecycle = true;
            return {
                success: false,
                error: ErrorType.PAGE_ERROR,
                error_message: 'Formulario no apareció tras validar PIN',
                return_pin: true,
                product_name: productName, nickname: '', diamonds: 0,
            };
        }

        stepLog('form-visible');

        // ─── PASO 2: Llenar formulario ───
        const formData = {
            name: CONFIG.REDEEM_NAME,
            bornAt: CONFIG.REDEEM_BORN_AT,
            gameId: gameAccountId,
        };

        await page.evaluate((data) => {
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
            function setVal(el, val) {
                setter.call(el, val);
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new Event('keyup', { bubbles: true }));
            }

            const nameEl = document.querySelector('#Name');
            if (nameEl) setVal(nameEl, data.name);

            const bornEl = document.querySelector('#BornAt');
            if (bornEl) { bornEl.focus(); setVal(bornEl, data.bornAt); }

            const idEl = document.querySelector('#GameAccountId');
            if (idEl) setVal(idEl, data.gameId);
        }, formData);

        // Nacionalidad: esperar opciones (máx 3s), fallback inyectar
        const nationality = CONFIG.REDEEM_NATIONALITY;
        await page.waitForFunction(
            () => {
                const sel = document.querySelector('#NationalityAlphaCode') ||
                            document.querySelector('[name="Customer.NationalityAlphaCode"]');
                return sel && sel.options.length > 1;
            },
            { timeout: 3000, polling: 100 }
        ).catch(() => {});

        await page.evaluate((nat) => {
            const sel = document.querySelector('#NationalityAlphaCode') ||
                        document.querySelector('[name="Customer.NationalityAlphaCode"]');
            if (!sel) return;

            let found = false;
            for (const opt of sel.options) {
                if (opt.value === nat || opt.text.toLowerCase().includes('chile')) {
                    sel.value = opt.value;
                    found = true;
                    break;
                }
            }
            if (!found) {
                const opt = document.createElement('option');
                opt.value = nat;
                opt.text = nat;
                sel.appendChild(opt);
                sel.value = nat;
            }
            sel.dispatchEvent(new Event('change', { bubbles: true }));
        }, nationality);

        // Checkboxes (privacy, etc)
        await page.evaluate(() => {
            document.querySelectorAll('input[type="checkbox"]').forEach(cb => {
                if (!cb.checked) {
                    cb.checked = true;
                    cb.dispatchEvent(new Event('change', { bubbles: true }));
                }
            });
        });

        stepLog('form-filled');

        // ─── PASO 3: Verificar cuenta ───
        let nickname = '';

        await page.evaluate(() => {
            document.querySelectorAll('#btn-verify, #btn-verify-account, .btn-verify')
                .forEach(btn => btn.removeAttribute('disabled'));
        });

        const hasVerifyBtn = await page.evaluate(
            () => Boolean(document.querySelector('#btn-verify'))
        );

        if (hasVerifyBtn) {
            for (let attempt = 0; attempt < 3; attempt++) {
                try {
                    const accountPromise = page.waitForResponse(
                        r => r.url().includes('validate/account'),
                        { timeout: 10000 }
                    );
                    await page.evaluate(() => {
                        const btn = document.querySelector('#btn-verify');
                        if (btn) { btn.removeAttribute('disabled'); btn.click(); }
                    });
                    const accountResp = await accountPromise;
                    const accountJson = await accountResp.json();

                    if (accountJson.Success) {
                        nickname = accountJson.Username || '';
                        break;
                    }

                    const msg = accountJson.Message || 'ID inválido';
                    if (msg.toLowerCase().includes('interno') || msg.toLowerCase().includes('internal')) {
                        await sleep(500);
                        continue;
                    }

                    shouldRecycle = true;
                    return {
                        success: false,
                        error: ErrorType.INVALID_ID,
                        error_message: msg,
                        return_pin: true,
                        product_name: productName, nickname: '', diamonds: 0,
                    };
                } catch (err) {
                    if (attempt === 2) {
                        fastify.log.warn({ err }, 'Verify account falló tras 3 intentos');
                    }
                    await sleep(300);
                }
            }
        }

        stepLog('verify-done');

        // ─── PASO 4: Canjear (confirm) ───
        await page.evaluate(() => {
            document.querySelectorAll(
                '[class*="cookie"], [class*="consent"], [class*="overlay"], [class*="backdrop"]'
            ).forEach(el => {
                if (!el.closest('.card')) el.remove();
            });
            const btn = document.querySelector('#btn-redeem');
            if (btn) btn.removeAttribute('disabled');
        });

        redeemClicked = true; // Desde aquí el PIN puede estar consumido

        let confirmOk = false;
        let confirmBody = '';

        // Intento 1: Click #btn-redeem
        try {
            const confirmPromise = page.waitForResponse(
                r => r.url().includes('/confirm'),
                { timeout: 15000 }
            );
            await page.evaluate(() => document.querySelector('#btn-redeem').click());
            const confirmResp = await confirmPromise;
            confirmBody = await confirmResp.text().catch(() => '');
            if (confirmResp.status() < 400) confirmOk = true;
        } catch (err) {
            fastify.log.warn({ err }, 'Confirm intento 1 falló');
        }

        // Intento 2: reCAPTCHA manual + click (si intento 1 falló)
        if (!confirmOk && !confirmBody) {
            try {
                const sitekey = await page.evaluate(() => {
                    const el = document.querySelector('[data-sitekey]');
                    if (el) return el.getAttribute('data-sitekey');
                    const scripts = document.querySelectorAll('script[src*="recaptcha"]');
                    for (const s of scripts) {
                        const m = s.src.match(/render=([^&]+)/);
                        if (m) return m[1];
                    }
                    const html = document.documentElement.innerHTML;
                    const m = html.match(/6L[a-zA-Z0-9_-]{38,}/);
                    return m ? m[0] : null;
                });

                if (sitekey) {
                    const confirmPromise = page.waitForResponse(
                        r => r.url().includes('/confirm'),
                        { timeout: 15000 }
                    );
                    await page.evaluate((key) => new Promise(resolve => {
                        window.grecaptcha.execute(key, { action: 'confirm' }).then(token => {
                            const input = document.querySelector('#g-recaptcha-response') ||
                                          document.querySelector('textarea[name="g-recaptcha-response"]');
                            if (input) { input.value = token; input.innerHTML = token; }
                            const btn = document.querySelector('#btn-redeem');
                            if (btn) { btn.removeAttribute('disabled'); btn.click(); }
                            resolve(true);
                        }).catch(() => resolve(false));
                    }), sitekey);
                    const confirmResp = await confirmPromise;
                    confirmBody = await confirmResp.text().catch(() => '');
                    if (confirmResp.status() < 400) confirmOk = true;
                }
            } catch (err) {
                fastify.log.warn({ err }, 'Confirm intento 2 falló');
            }
        }

        if (!confirmOk && !confirmBody) {
            shouldRecycle = true;
            return {
                success: false,
                error: ErrorType.UNKNOWN,
                error_message: 'No se pudo enviar el formulario de canje',
                return_pin: false, // PIN posiblemente consumido
                product_name: productName, nickname, diamonds: 0,
            };
        }

        shouldRecycle = true;

        stepLog('confirm-sent');

        // ─── PASO 5: Evaluar resultado ───
        pageText = await page.innerText('body');
        lowerText = pageText.toLowerCase();
        const combinedText = `${lowerText} ${confirmBody.toLowerCase()}`;

        // Success keywords
        const successKw = SUCCESS_KEYWORDS.find(kw => combinedText.includes(kw));
        if (successKw) {
            return {
                success: true,
                error: ErrorType.NONE, error_message: '',
                return_pin: false,
                product_name: productName, nickname,
                diamonds: parseDiamonds(productName),
            };
        }

        // Confirm JSON
        if (confirmOk && confirmBody) {
            try {
                const json = JSON.parse(confirmBody);
                if (json && typeof json === 'object') {
                    if (json.Success === true) {
                        return {
                            success: true,
                            error: ErrorType.NONE, error_message: '',
                            return_pin: false,
                            product_name: productName, nickname,
                            diamonds: parseDiamonds(productName),
                        };
                    }
                    return {
                        success: false,
                        error: classifyError('confirm', json.Message),
                        error_message: json.Message || 'Error del servidor',
                        return_pin: false,
                        product_name: productName, nickname, diamonds: 0,
                    };
                }
            } catch {}

            // Non-JSON body sin errores = éxito
            const hasError = CONFIRM_ERROR_KEYWORDS.some(kw => confirmBody.toLowerCase().includes(kw));
            if (!hasError) {
                return {
                    success: true,
                    error: ErrorType.NONE, error_message: '',
                    return_pin: false,
                    product_name: productName, nickname,
                    diamonds: parseDiamonds(productName),
                };
            }
        }

        // Form still visible
        if (STILL_ON_FORM_KEYWORDS.some(kw => lowerText.includes(kw))) {
            return {
                success: false,
                error: ErrorType.UNKNOWN,
                error_message: 'Formulario sigue visible - canje no completado',
                return_pin: false,
                product_name: productName, nickname, diamonds: 0,
            };
        }

        // Resultado incierto
        return {
            success: false,
            error: ErrorType.UNKNOWN,
            error_message: 'Resultado incierto – no se confirmó el canje',
            return_pin: false,
            product_name: productName, nickname, diamonds: 0,
        };

    } catch (err) {
        fastify.log.error({ err }, 'Error de automatización');
        return {
            success: false,
            error: ErrorType.UNKNOWN,
            error_message: err.message || String(err),
            return_pin: !redeemClicked,
            product_name: '', nickname: '', diamonds: 0,
        };
    } finally {
        const elapsed = Date.now() - startMs;
        fastify.log.info({ pin: pin.slice(0, 8), elapsedMs: elapsed }, 'Canje completado');

        if (entry) {
            if (shouldRecycle) {
                recyclePage(entry);
            } else {
                try { await entry.context.close(); } catch {}
                fillPool().catch(() => {});
            }
        }
    }
}

// ═══════════════════════════════════════════════════════════════════════════
// Task management
// ═══════════════════════════════════════════════════════════════════════════

const tasks = new Map();
const TASK_TTL = 600_000; // 10 minutos

function generateTaskId() {
    return crypto.randomUUID().slice(0, 8);
}

function cleanupTasks() {
    const now = Date.now();
    for (const [id, task] of tasks) {
        if (now - task._createdAt > TASK_TTL && ['success', 'failed'].includes(task.status)) {
            tasks.delete(id);
        }
    }
}

function sanitizeTask(task) {
    const { _createdAt, ...clean } = task;
    return clean;
}

// ═══════════════════════════════════════════════════════════════════════════
// Webhook
// ═══════════════════════════════════════════════════════════════════════════

async function sendWebhook(url, data) {
    if (!url) return;
    try {
        const resp = await fetch(url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Api-Key': CONFIG.API_SECRET_KEY,
            },
            body: JSON.stringify(data),
            signal: AbortSignal.timeout(10000),
        });
        fastify.log.info({ url, status: resp.status }, 'Webhook enviado');
    } catch (err) {
        fastify.log.error({ err, url }, 'Error enviando webhook');
    }
}

// ═══════════════════════════════════════════════════════════════════════════
// Auth
// ═══════════════════════════════════════════════════════════════════════════

function verifyApiKey(request, reply) {
    const key = request.headers['x-api-key'];
    if (key !== CONFIG.API_SECRET_KEY) {
        reply.code(401).send({ error: 'API key inválida' });
        return false;
    }
    return true;
}

// ═══════════════════════════════════════════════════════════════════════════
// Build response (compatible con RedeemResponse de Python)
// ═══════════════════════════════════════════════════════════════════════════

function buildResponse(taskId, req, result, elapsedMs) {
    return {
        task_id: taskId,
        status: result.success ? 'success' : 'failed',
        pin: req.pin || '',
        game_account_id: req.game_account_id || '',
        nickname: result.nickname || '',
        product_name: result.product_name || '',
        diamonds: result.diamonds || 0,
        redeemed_at: result.success ? new Date().toISOString() : '',
        order_id: req.order_id || '',
        error: result.error || '',
        error_message: result.error_message || '',
        return_pin: result.return_pin || false,
        redeem_duration_ms: elapsedMs || 0,
    };
}

// ═══════════════════════════════════════════════════════════════════════════
// Routes (misma API que el server.py de HypeAuto Python)
// ═══════════════════════════════════════════════════════════════════════════

// POST /redeem — async, retorna task_id inmediatamente
fastify.post('/redeem', async (request, reply) => {
    if (!verifyApiKey(request, reply)) return;

    const { pin, game_account_id, order_id = '', webhook_url = '' } = request.body || {};
    if (!pin || !game_account_id) {
        return reply.code(400).send({ error: 'pin y game_account_id son requeridos' });
    }

    const taskId = generateTaskId();
    const taskResp = {
        task_id: taskId,
        status: 'queued',
        pin,
        game_account_id,
        order_id,
        nickname: '',
        product_name: '',
        diamonds: 0,
        redeemed_at: '',
        error: '',
        error_message: '',
        return_pin: false,
        redeem_duration_ms: 0,
        _createdAt: Date.now(),
    };
    tasks.set(taskId, taskResp);
    cleanupTasks();

    fastify.log.info({ taskId, pin: pin.slice(0, 8), gameAccountId: game_account_id }, 'Encolado');

    // Process in background (fire-and-forget)
    (async () => {
        const release = await redeemSemaphore.acquire();
        try {
            const current = tasks.get(taskId);
            if (current) current.status = 'processing';

            const t0 = Date.now();
            const result = await automateRedeem(pin, game_account_id);
            const elapsed = Date.now() - t0;

            const resp = buildResponse(taskId, { pin, game_account_id, order_id }, result, elapsed);
            resp._createdAt = taskResp._createdAt;
            tasks.set(taskId, resp);

            // Webhook
            const whUrl = webhook_url || CONFIG.WEBHOOK_URL;
            if (whUrl) sendWebhook(whUrl, sanitizeTask(resp));
        } catch (err) {
            fastify.log.error({ err, taskId }, 'Error procesando tarea');
            const current = tasks.get(taskId);
            if (current) {
                current.status = 'failed';
                current.error = 'unknown';
                current.error_message = `Error interno: ${err.message}`;
                current.return_pin = true;
            }
        } finally {
            release();
        }
    })();

    return sanitizeTask(taskResp);
});

// POST /redeem/sync — espera resultado antes de responder
fastify.post('/redeem/sync', async (request, reply) => {
    if (!verifyApiKey(request, reply)) return;

    const { pin, game_account_id, order_id = '', webhook_url = '' } = request.body || {};
    if (!pin || !game_account_id) {
        return reply.code(400).send({ error: 'pin y game_account_id son requeridos' });
    }

    const taskId = generateTaskId();
    fastify.log.info({ taskId, pin: pin.slice(0, 8) }, 'SYNC redeem');

    const release = await redeemSemaphore.acquire();
    let result;
    const t0 = Date.now();
    try {
        result = await automateRedeem(pin, game_account_id);
    } finally {
        release();
    }
    const elapsed = Date.now() - t0;

    const resp = buildResponse(taskId, { pin, game_account_id, order_id }, result, elapsed);

    const whUrl = webhook_url || CONFIG.WEBHOOK_URL;
    if (whUrl) sendWebhook(whUrl, resp);

    return resp;
});

// GET /task/:taskId — consultar estado
fastify.get('/task/:taskId', async (request, reply) => {
    if (!verifyApiKey(request, reply)) return;

    const task = tasks.get(request.params.taskId);
    if (!task) {
        return reply.code(404).send({ error: 'Tarea no encontrada' });
    }
    return sanitizeTask(task);
});

// POST /redeem/batch — múltiples PINs
fastify.post('/redeem/batch', async (request, reply) => {
    if (!verifyApiKey(request, reply)) return;

    const items = request.body;
    if (!Array.isArray(items)) {
        return reply.code(400).send({ error: 'Se espera un array' });
    }

    const responses = [];
    for (const item of items) {
        const taskId = generateTaskId();
        const taskResp = {
            task_id: taskId,
            status: 'queued',
            pin: item.pin,
            game_account_id: item.game_account_id,
            order_id: item.order_id || '',
            nickname: '', product_name: '', diamonds: 0,
            redeemed_at: '', error: '', error_message: '',
            return_pin: false, redeem_duration_ms: 0,
            _createdAt: Date.now(),
        };
        tasks.set(taskId, taskResp);

        (async () => {
            const release = await redeemSemaphore.acquire();
            try {
                const current = tasks.get(taskId);
                if (current) current.status = 'processing';

                const t0 = Date.now();
                const result = await automateRedeem(item.pin, item.game_account_id);
                const elapsed = Date.now() - t0;

                const resp = buildResponse(taskId, item, result, elapsed);
                resp._createdAt = taskResp._createdAt;
                tasks.set(taskId, resp);

                const whUrl = item.webhook_url || CONFIG.WEBHOOK_URL;
                if (whUrl) sendWebhook(whUrl, sanitizeTask(resp));
            } catch (err) {
                const current = tasks.get(taskId);
                if (current) {
                    current.status = 'failed';
                    current.error = 'unknown';
                    current.error_message = err.message;
                    current.return_pin = true;
                }
            } finally {
                release();
            }
        })();

        responses.push(sanitizeTask(taskResp));
    }

    return responses;
});

// GET /health
fastify.get('/health', async () => {
    let activeCount = 0;
    let queuedCount = 0;
    for (const task of tasks.values()) {
        if (task.status === 'processing') activeCount++;
        if (task.status === 'queued') queuedCount++;
    }

    return {
        status: 'ok',
        queue_size: queuedCount,
        active_tasks: activeCount,
        max_concurrent: CONFIG.MAX_CONCURRENT,
        pool_size: pagePool.length,
        browser_ready: isBrowserReady(),
    };
});

// GET /metrics
fastify.get('/metrics', async () => {
    const mem = process.memoryUsage();
    return {
        rss_mb: +(mem.rss / 1024 / 1024).toFixed(1),
        heap_used_mb: +(mem.heapUsed / 1024 / 1024).toFixed(1),
        browser_connected: isBrowserReady(),
        pool_size: pagePool.length,
        active_contexts: redeemSemaphore.current,
        max_concurrent: CONFIG.MAX_CONCURRENT,
        total_tasks: tasks.size,
    };
});

// ═══════════════════════════════════════════════════════════════════════════
// Lifecycle
// ═══════════════════════════════════════════════════════════════════════════

fastify.addHook('onClose', async () => {
    for (const entry of pagePool) {
        try { await entry.context.close(); } catch {}
    }
    pagePool.length = 0;
    await closeBrowser();
});

function throttleGpuProcess() {
    // renice 19 = prioridad más baja. GPU process (SwiftShader) sigue vivo
    // para reCAPTCHA pero cede CPU a otros procesos cuando están activos.
    const { execSync } = require('child_process');
    try {
        const pids = execSync("pgrep -f 'type=gpu-process'", { encoding: 'utf8' }).trim();
        if (pids) {
            for (const pid of pids.split('\n')) {
                execSync(`renice 19 -p ${pid}`, { stdio: 'ignore' });
            }
            fastify.log.info({ pids: pids.split('\n') }, 'GPU process → nice 19 (prioridad mínima)');
        }
    } catch {}
}

async function start() {
    try {
        await ensureBrowser();
        await fillPool();
        fastify.log.info({ poolSize: pagePool.length }, 'Pool de páginas listo');

        // Bajar prioridad del GPU process (SwiftShader) para que no robe CPU
        setTimeout(throttleGpuProcess, 3000);

        await fastify.listen({ port: CONFIG.PORT, host: '0.0.0.0' });
    } catch (err) {
        fastify.log.error({ err }, 'Error iniciando servidor');
        process.exit(1);
    }
}

async function shutdown(signal) {
    fastify.log.info({ signal }, 'Cerrando HypeAuto...');
    try { await fastify.close(); process.exit(0); }
    catch { process.exit(1); }
}

process.on('SIGINT', () => void shutdown('SIGINT'));
process.on('SIGTERM', () => void shutdown('SIGTERM'));

void start();
