/**
 * ============================================================================
 *  ARCHIVO DE PRUEBA / DIAGNÓSTICO — NO ES PARTE DEL BOT EN PRODUCCIÓN
 * ============================================================================
 *
 *  CONTEXTO HISTóRICO (sesión 30/abr/2026):
 *  Se creó para investigar un spike de errores 'Formulario no apareció tras
 *  validar PIN' que empezó el 28/abr/2026 sin cambios en el bot. La causa real
 *  fue un cambio en el sitio https://redeem.hype.games/ que hacía que el form
 *  tardara más en renderizar.
 *
 *  RESOLUCIÓN:
 *  Los fixes finales viven en main.js (commits ebd3b19 y 1f161a1 de la rama
 *  hypeauto-fast). Este script se conserva como herramienta de diagnóstico
 *  para futuras regresiones del mismo tipo.
 *
 *  NUNCA ejecutar automáticamente, ni añadir a package.json scripts.
 *  Tampoco subirlo al VPS como parte del despliegue.
 *
 * ----------------------------------------------------------------------------
 * diagnose.js — Reproduce el error "Formulario no apareció tras validar PIN"
 *
 * - Replica EXACTAMENTE el flujo de main.js (mismo user-agent, headless, args).
 * - ABORTA antes de enviar el formulario → el PIN NO se consume (se queda en estado
 *   "validado pero no canjeado", que Hype permite reintentar).
 * - Loop hasta MAX_ITERS o hasta que detecte el fallo.
 * - Cuando falla: guarda screenshot + HTML + traza de red en ./diagnose-out/
 *
 * Uso:
 *   node diagnose.js <PIN> <GAME_ACCOUNT_ID> [MAX_ITERS]
 */

const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const PIN = process.argv[2];
const GAME_ID = process.argv[3];
const MAX_ITERS = parseInt(process.argv[4] || '100', 10);

if (!PIN || !GAME_ID) {
    console.error('Uso: node diagnose.js <PIN> <GAME_ACCOUNT_ID> [MAX_ITERS]');
    process.exit(1);
}

const OUT_DIR = path.join(__dirname, 'diagnose-out');
fs.mkdirSync(OUT_DIR, { recursive: true });

const URL = 'https://redeem.hype.games/';
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function runIteration(browser, iter) {
    const t0 = Date.now();
    const log = (msg, extra = {}) => {
        const ms = Date.now() - t0;
        console.log(`[iter ${String(iter).padStart(3, '0')} +${String(ms).padStart(5)}ms] ${msg}`, Object.keys(extra).length ? JSON.stringify(extra) : '');
    };

    const context = await browser.newContext({
        userAgent: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/120.0.0.0 Safari/537.36',
        viewport: { width: 1280, height: 720 },
    });
    const page = await context.newPage();

    // Capturar tráfico de red
    const networkLog = [];
    page.on('request', req => networkLog.push({ t: Date.now() - t0, type: 'req', method: req.method(), url: req.url() }));
    page.on('response', async res => {
        const entry = { t: Date.now() - t0, type: 'res', status: res.status(), url: res.url() };
        if (res.url().includes('/validate') || res.url().includes('/account')) {
            try { entry.body = (await res.text()).slice(0, 2000); } catch {}
        }
        networkLog.push(entry);
    });
    page.on('pageerror', err => networkLog.push({ t: Date.now() - t0, type: 'pageerror', msg: err.message }));

    try {
        log('navigate');
        await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 30000 });

        log('wait #pininput');
        await page.waitForSelector('#pininput', { state: 'visible', timeout: 15000 });

        log('fill pin');
        await page.evaluate((p) => {
            const el = document.querySelector('#pininput');
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
            setter.call(el, p);
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }, PIN);

        log('wait recaptcha');
        await page.waitForFunction(
            () => { const b = document.querySelector('#btn-validate'); return b && !b.disabled; },
            { timeout: 30000, polling: 100 }
        );

        log('click validate');
        const validatePromise = page.waitForResponse(
            r => r.url().includes('/validate') && !r.url().includes('account'),
            { timeout: 30000 }
        ).catch((e) => ({ _timeout: true, err: e.message }));

        await page.evaluate(() => document.querySelector('#btn-validate').click());
        const validateResp = await validatePromise;

        const validateInfo = validateResp._timeout
            ? { timeout: true }
            : { status: validateResp.status(), url: validateResp.url() };
        log('validate response', validateInfo);

        // Esperar card flip
        try {
            await page.locator('.card.back').waitFor({ state: 'visible', timeout: 15000 });
            log('card-flipped');
        } catch {
            log('card-flip TIMEOUT');
        }

        // Esperar formulario
        let formAppeared = false;
        const waitFormStart = Date.now();
        try {
            await page.waitForSelector('#GameAccountId', { state: 'visible', timeout: 15000 });
            formAppeared = true;
            log('FORM-VISIBLE', { ms: Date.now() - waitFormStart });
        } catch {
            log('FORM-MISSING after 15s waitForSelector');
            // Reintento como en main.js
            await sleep(1000);
            formAppeared = await page.evaluate(() => !!document.querySelector('#GameAccountId'));
            log('FORM after retry', { found: formAppeared });
        }

        if (!formAppeared) {
            // ⚠ ÉXITO DEL DIAGNÓSTICO: capturamos el fallo
            console.log(`\n🎯 FALLO REPRODUCIDO en iter ${iter} — capturando evidencia...\n`);

            const stamp = `${Date.now()}_iter${iter}`;
            const pageText = await page.innerText('body').catch(() => '');
            const html = await page.content().catch(() => '');

            await page.screenshot({ path: path.join(OUT_DIR, `${stamp}_screenshot.png`), fullPage: true }).catch(() => {});
            fs.writeFileSync(path.join(OUT_DIR, `${stamp}_page.html`), html);
            fs.writeFileSync(path.join(OUT_DIR, `${stamp}_body-text.txt`), pageText);
            fs.writeFileSync(path.join(OUT_DIR, `${stamp}_network.json`), JSON.stringify(networkLog, null, 2));

            // Inspección DOM
            const domInfo = await page.evaluate(() => ({
                hasCard: !!document.querySelector('.card'),
                hasCardBack: !!document.querySelector('.card.back'),
                hasGameAccountId: !!document.querySelector('#GameAccountId'),
                hasForm: !!document.querySelector('form'),
                hasErrorMsg: !!document.querySelector('.error, .alert, .swal2-popup'),
                bodyLen: document.body.innerHTML.length,
                title: document.title,
                visibleErrorTexts: Array.from(document.querySelectorAll('.error, .alert, .swal2-popup, [class*="error"]'))
                    .map(e => e.innerText.trim()).filter(Boolean).slice(0, 5),
            }));
            fs.writeFileSync(path.join(OUT_DIR, `${stamp}_dom-info.json`), JSON.stringify(domInfo, null, 2));

            console.log('📂 Archivos guardados en:', OUT_DIR);
            console.log('📊 DOM:', JSON.stringify(domInfo, null, 2));
            console.log('📊 Texto visible (primeros 500 chars):', pageText.slice(0, 500));

            return { failed: true, iter };
        }

        log('OK — abortando antes de llenar form (PIN no consumido)');
        return { failed: false };

    } finally {
        await context.close().catch(() => {});
    }
}

(async () => {
    console.log(`\n🔍 Diagnóstico hypeauto — PIN ${PIN.slice(0, 8)}... ID ${GAME_ID} — hasta ${MAX_ITERS} intentos\n`);
    console.log(`📂 Output: ${OUT_DIR}\n`);

    const browser = await chromium.launch({
        headless: true,
        args: [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--disable-gpu',
        ],
    });

    let stats = { success: 0, failed: 0 };
    for (let i = 1; i <= MAX_ITERS; i++) {
        try {
            const r = await runIteration(browser, i);
            if (r.failed) {
                stats.failed++;
                console.log(`\n✅ Fallo capturado en iter ${i}. Total exitosos antes: ${stats.success}`);
                break;
            }
            stats.success++;
        } catch (err) {
            console.error(`[iter ${i}] ❌ Excepción:`, err.message);
            stats.failed++;
        }
        // Pequeña pausa entre iteraciones
        await sleep(500);
    }

    console.log(`\n📊 Resumen: ${stats.success} exitosos, ${stats.failed} fallidos de ${stats.success + stats.failed} intentos`);
    await browser.close();
})();
