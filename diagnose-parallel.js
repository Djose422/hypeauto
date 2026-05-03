/**
 * ============================================================================
 *  ARCHIVO DE PRUEBA / DIAGNÓSTICO — NO ES PARTE DEL BOT EN PRODUCCIÓN
 * ============================================================================
 *
 *  CONTEXTO HISTóRICO (sesión 30/abr/2026):
 *  Versión paralela de diagnose.js. Se subió al VPS y se ejecutó con 5
 *  workers × 200 iteraciones (200/200 OK) para descartar que la IP del VPS
 *  o la concurrencia fueran la causa del bug. Confirmado: el problema
 *  estaba en los timeouts del bot, no en la red ni en la carga.
 *
 *  RESOLUCIÓN:
 *  Fixes en main.js, commits ebd3b19 y 1f161a1 (rama hypeauto-fast).
 *  Este script se conserva por si hay que repetir el experimento.
 *
 *  NO ejecutar en producción con el bot activo (consume CPU/RAM y compite
 *  por el pool de páginas). Para usarlo: 'systemctl stop hypeauto.service'
 *  primero, ejecutar, y luego 'systemctl start hypeauto.service'.
 *
 * ----------------------------------------------------------------------------
 * diagnose-parallel.js — Reproduce el error con N navegadores concurrentes
 * Diseñado para correr en el VPS (misma IP que el bot real).
 */

const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const PIN = process.argv[2];
const GAME_ID = process.argv[3];
const MAX_ITERS = parseInt(process.argv[4] || '200', 10);
const PARALLEL = parseInt(process.argv[5] || '5', 10);

if (!PIN || !GAME_ID) {
    console.error('Uso: node diagnose-parallel.js <PIN> <GAME_ID> [MAX_ITERS] [PARALLEL]');
    process.exit(1);
}

const OUT_DIR = path.join(__dirname, 'diagnose-out');
fs.mkdirSync(OUT_DIR, { recursive: true });

const URL = 'https://redeem.hype.games/';
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

let stop = false;
let stats = { success: 0, failed: 0, exception: 0 };
let iterCounter = 0;

async function runIteration(browser, iter, workerId) {
    const t0 = Date.now();
    const log = (msg, extra = {}) => {
        const ms = Date.now() - t0;
        console.log(`[w${workerId} iter ${String(iter).padStart(4, '0')} +${String(ms).padStart(5)}ms] ${msg}`, Object.keys(extra).length ? JSON.stringify(extra) : '');
    };

    const context = await browser.newContext({
        userAgent: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/120.0.0.0 Safari/537.36',
        viewport: { width: 1280, height: 720 },
    });
    const page = await context.newPage();

    const networkLog = [];
    page.on('request', req => networkLog.push({ t: Date.now() - t0, type: 'req', method: req.method(), url: req.url() }));
    page.on('response', async res => {
        const entry = { t: Date.now() - t0, type: 'res', status: res.status(), url: res.url() };
        if (res.url().includes('/validate') || res.url().includes('/account')) {
            try {
                entry.body = (await res.text()).slice(0, 3000);
                entry.headers = res.headers();
            } catch {}
        }
        networkLog.push(entry);
    });
    page.on('pageerror', err => networkLog.push({ t: Date.now() - t0, type: 'pageerror', msg: err.message }));

    try {
        await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 30000 });
        await page.waitForSelector('#pininput', { state: 'visible', timeout: 15000 });

        await page.evaluate((p) => {
            const el = document.querySelector('#pininput');
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
            setter.call(el, p);
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }, PIN);

        await page.waitForFunction(
            () => { const b = document.querySelector('#btn-validate'); return b && !b.disabled; },
            { timeout: 30000, polling: 100 }
        );

        const validatePromise = page.waitForResponse(
            r => r.url().includes('/validate') && !r.url().includes('account'),
            { timeout: 30000 }
        ).catch((e) => ({ _timeout: true, err: e.message }));

        await page.evaluate(() => document.querySelector('#btn-validate').click());
        const validateResp = await validatePromise;

        const validateStatus = validateResp._timeout ? 'TIMEOUT' : validateResp.status();

        try {
            await page.locator('.card.back').waitFor({ state: 'visible', timeout: 15000 });
        } catch {}

        let formAppeared = false;
        try {
            await page.waitForSelector('#GameAccountId', { state: 'visible', timeout: 15000 });
            formAppeared = true;
        } catch {
            await sleep(1000);
            formAppeared = await page.evaluate(() => !!document.querySelector('#GameAccountId'));
        }

        if (!formAppeared) {
            console.log(`\n🎯🎯🎯 FALLO REPRODUCIDO en iter ${iter} (worker ${workerId}) — validate=${validateStatus} — capturando evidencia...\n`);

            const stamp = `${Date.now()}_w${workerId}_iter${iter}`;
            const pageText = await page.innerText('body').catch(() => '');
            const html = await page.content().catch(() => '');

            await page.screenshot({ path: path.join(OUT_DIR, `${stamp}_screenshot.png`), fullPage: true }).catch(() => {});
            fs.writeFileSync(path.join(OUT_DIR, `${stamp}_page.html`), html);
            fs.writeFileSync(path.join(OUT_DIR, `${stamp}_body-text.txt`), pageText);
            fs.writeFileSync(path.join(OUT_DIR, `${stamp}_network.json`), JSON.stringify(networkLog, null, 2));

            const domInfo = await page.evaluate(() => ({
                hasCard: !!document.querySelector('.card'),
                hasCardBack: !!document.querySelector('.card.back'),
                hasGameAccountId: !!document.querySelector('#GameAccountId'),
                hasForm: !!document.querySelector('form'),
                hasErrorMsg: !!document.querySelector('.error, .alert, .swal2-popup'),
                bodyLen: document.body.innerHTML.length,
                title: document.title,
                visibleErrorTexts: Array.from(document.querySelectorAll('.error, .alert, .swal2-popup, [class*="error"]'))
                    .map(e => e.innerText.trim()).filter(Boolean).slice(0, 10),
                cardBackHTML: (document.querySelector('.card.back')?.innerHTML || '').slice(0, 1000),
            }));
            fs.writeFileSync(path.join(OUT_DIR, `${stamp}_dom-info.json`), JSON.stringify(domInfo, null, 2));

            console.log('📂 Archivos:', OUT_DIR);
            console.log('📊 DOM:', JSON.stringify(domInfo, null, 2));
            console.log('📊 Validate status:', validateStatus);
            console.log('📊 Texto visible (500c):', pageText.slice(0, 500));

            stop = true;
            return { failed: true };
        }

        log(`OK validate=${validateStatus}`);
        return { failed: false };

    } catch (err) {
        log(`EXCEPCIÓN: ${err.message.slice(0, 200)}`);
        return { exception: true };
    } finally {
        await context.close().catch(() => {});
    }
}

async function worker(browser, workerId) {
    while (!stop && iterCounter < MAX_ITERS) {
        iterCounter++;
        const myIter = iterCounter;
        try {
            const r = await runIteration(browser, myIter, workerId);
            if (r.failed) stats.failed++;
            else if (r.exception) stats.exception++;
            else stats.success++;
        } catch (err) {
            stats.exception++;
            console.error(`[w${workerId}] outer err: ${err.message}`);
        }
        await sleep(200);
    }
}

(async () => {
    console.log(`\n🔍 Diagnóstico PARALELO — PIN ${PIN.slice(0, 8)}... ID ${GAME_ID}`);
    console.log(`   Workers: ${PARALLEL} | Max iters: ${MAX_ITERS} | Output: ${OUT_DIR}\n`);

    const browser = await chromium.launch({
        headless: true,
        args: [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--disable-gpu',
        ],
    });

    const workers = [];
    for (let i = 1; i <= PARALLEL; i++) {
        workers.push(worker(browser, i));
    }
    await Promise.all(workers);

    console.log(`\n📊 RESUMEN FINAL: ${stats.success} OK / ${stats.failed} fallos / ${stats.exception} excepciones (de ${iterCounter} totales)`);
    await browser.close();
})();
