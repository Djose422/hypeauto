// inspect-hype-flow.js — Análisis completo del flujo de canje en redeem.hype.games
// USO: node inspect-hype-flow.js
// CONSUME EL PIN. Genera ./hype-trace.json con todo el detalle.

const { chromium } = require('playwright');
const fs = require('fs');

const PIN = 'C61E2F55-E9A9-47CA-8349-B69B12C447AA';
const GAME_ID = '2643864116';
const URL = 'https://redeem.hype.games/';

const trace = {
    startedAt: new Date().toISOString(),
    pin: PIN,
    gameAccountId: GAME_ID,
    requests: [],
    responses: [],
    consoleMessages: [],
    pageErrors: [],
    domSnapshots: [],
    steps: [],
};

function step(name, data = {}) {
    const entry = { t: Date.now(), name, ...data };
    trace.steps.push(entry);
    console.log(`[${new Date().toISOString().slice(11, 23)}] ${name}`, Object.keys(data).length ? data : '');
}

(async () => {
    const browser = await chromium.launch({ headless: false, slowMo: 100 });
    const ctx = await browser.newContext({
        userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        viewport: { width: 1280, height: 900 },
    });
    const page = await ctx.newPage();

    // Hooks
    page.on('request', req => {
        const u = req.url();
        if (u.startsWith('data:') || u.includes('google') || u.includes('recaptcha')) return;
        trace.requests.push({
            t: Date.now(),
            method: req.method(),
            url: u,
            resourceType: req.resourceType(),
            headers: req.headers(),
            postData: req.postData() || null,
        });
    });

    page.on('response', async resp => {
        const u = resp.url();
        if (u.startsWith('data:') || u.includes('google') || u.includes('recaptcha')) return;
        let body = null;
        const ct = resp.headers()['content-type'] || '';
        if (ct.includes('json') || ct.includes('text') || u.includes('/api/')) {
            try { body = (await resp.text()).slice(0, 4000); } catch {}
        }
        trace.responses.push({
            t: Date.now(),
            status: resp.status(),
            url: u,
            headers: resp.headers(),
            body,
        });
    });

    page.on('console', msg => trace.consoleMessages.push({ t: Date.now(), type: msg.type(), text: msg.text() }));
    page.on('pageerror', err => trace.pageErrors.push({ t: Date.now(), message: err.message, stack: err.stack }));

    const snap = async (label) => {
        try {
            const html = await page.content();
            const visibleText = await page.evaluate(() => document.body.innerText);
            const cookies = await ctx.cookies();
            const localStorage = await page.evaluate(() => Object.entries(window.localStorage));
            const sessionStorage = await page.evaluate(() => Object.entries(window.sessionStorage));
            trace.domSnapshots.push({
                t: Date.now(), label,
                url: page.url(),
                title: await page.title(),
                visibleText,
                htmlLength: html.length,
                cookies,
                localStorage,
                sessionStorage,
            });
        } catch (e) { console.log('snap err:', e.message); }
    };

    try {
        step('goto');
        await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 30000 });
        await page.waitForSelector('#pininput', { timeout: 15000 });
        await snap('after-load');

        step('inspect-pin-input');
        const pinAttrs = await page.evaluate(() => {
            const el = document.querySelector('#pininput');
            if (!el) return null;
            return {
                tag: el.tagName, type: el.type, name: el.name,
                maxLength: el.maxLength, autocomplete: el.autocomplete,
                attrs: Array.from(el.attributes).map(a => [a.name, a.value]),
                outer: el.outerHTML.slice(0, 500),
            };
        });
        step('pin-input-details', pinAttrs);

        step('fill-pin');
        await page.evaluate((p) => {
            const el = document.querySelector('#pininput');
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
            setter.call(el, p);
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }, PIN);

        step('wait-validate-enabled');
        await page.waitForFunction(() => {
            const b = document.querySelector('#btn-validate');
            return b && !b.disabled;
        }, { timeout: 15000, polling: 100 });

        step('inspect-validate-btn');
        const btnInfo = await page.evaluate(() => {
            const b = document.querySelector('#btn-validate');
            return {
                outer: b.outerHTML.slice(0, 500),
                onclick: typeof b.onclick,
            };
        });
        step('validate-btn-details', btnInfo);

        await snap('before-validate-click');

        step('click-validate');
        await page.evaluate(() => document.querySelector('#btn-validate').click());

        step('wait-form');
        await page.waitForSelector('#GameAccountId', { state: 'visible', timeout: 15000 });
        await snap('after-validate-form-visible');

        step('inspect-form');
        const formInfo = await page.evaluate(() => {
            const f = document.querySelector('#GameAccountId').closest('form');
            const fields = f ? Array.from(f.elements).map(e => ({
                name: e.name, id: e.id, type: e.type, required: e.required, value: e.value
            })) : [];
            return {
                formAction: f ? f.action : null,
                formMethod: f ? f.method : null,
                fields,
                hasVerifyBtn: !!document.querySelector('#btn-verify'),
                hasRedeemBtn: !!document.querySelector('#btn-redeem'),
            };
        });
        step('form-details', formInfo);

        step('fill-game-id');
        await page.fill('#GameAccountId', GAME_ID);

        const hasVerify = await page.$('#btn-verify');
        if (hasVerify) {
            step('click-verify');
            await page.evaluate(() => {
                const b = document.querySelector('#btn-verify');
                if (b) { b.removeAttribute('disabled'); b.click(); }
            });
            try {
                const r = await page.waitForResponse(rr => rr.url().includes('validate/account'), { timeout: 15000 });
                const json = await r.json().catch(() => null);
                step('verify-response', { status: r.status(), json });
            } catch (e) { step('verify-no-response', { err: e.message }); }
            await snap('after-verify');
        }

        step('click-redeem');
        await page.evaluate(() => {
            const b = document.querySelector('#btn-redeem');
            if (b) { b.removeAttribute('disabled'); b.click(); }
        });

        try {
            const r = await page.waitForResponse(rr => rr.url().includes('/confirm'), { timeout: 30000 });
            const body = await r.text().catch(() => '');
            step('confirm-response', { status: r.status(), body: body.slice(0, 1500) });
        } catch (e) { step('confirm-no-response', { err: e.message }); }

        await page.waitForTimeout(3000);
        await snap('final');
        step('done');
    } catch (err) {
        step('FATAL', { error: err.message, stack: err.stack });
        await snap('fatal');
    } finally {
        trace.endedAt = new Date().toISOString();
        fs.writeFileSync('./hype-trace.json', JSON.stringify(trace, null, 2));
        console.log('\n=== Trace guardado en ./hype-trace.json ===');
        console.log(`Requests: ${trace.requests.length}, Responses: ${trace.responses.length}, Steps: ${trace.steps.length}`);
        await browser.close();
    }
})();
