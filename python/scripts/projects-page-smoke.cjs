/* Exercise the real local page without modifying preferences or business data. */
const {createRequire} = require('node:module');
const path = require('node:path');
const fs = require('node:fs');

async function main() {
    const [checkout, storageState] = process.argv.slice(2);
    let chromium;
    try {
        ({chromium} = createRequire(path.join(checkout, 'package.json'))('playwright'));
        if (!fs.existsSync(process.env.SYMPHONY_SMOKE_BROWSER || chromium.executablePath())) {
            throw new Error('browser missing');
        }
    } catch (_) {
        console.log('Smoke environment unavailable: install the checkout Playwright dependencies and browser, or set SYMPHONY_SMOKE_BROWSER');
        return 125;
    }
    let browser, page;
    const failures = [];
    try {
        browser = await chromium.launch({headless: true,
            ...(process.env.SYMPHONY_SMOKE_BROWSER ? {executablePath: process.env.SYMPHONY_SMOKE_BROWSER} : {})});
        const context = await browser.newContext({storageState, ignoreHTTPSErrors: true});
        page = await context.newPage();
        const label = url => { const u = new URL(url); return u.origin + u.pathname; };
        page.on('response', response => {
            if (response.status() >= 400) failures.push(`${response.status()} ${label(response.url())}`);
        });
        page.on('requestfailed', request => {
            if (request.failure()?.errorText !== 'net::ERR_ABORTED') failures.push(`Request failed ${label(request.url())}`);
        });
        page.on('pageerror', () => failures.push('Uncaught page JavaScript error'));
        const query = page.waitForResponse(response =>
            new URL(response.url()).pathname === '/foyr/projects/apis/query', {timeout: 30000});
        // Attach a rejection handler while navigation is in flight.
        query.catch(() => {});
        await page.goto('https://127.0.0.1/foyr/tpm-ui?root=projects', {waitUntil: 'domcontentloaded', timeout: 30000});
        const result = await query;
        if (result.status() === 401 || result.status() === 403) {
            console.log('Smoke environment unavailable: login state or Projects eligibility is invalid');
            return 125;
        }
        if (!result.ok()) throw new Error('Projects query returned an error');
        const data = await result.json();
        if (!Array.isArray(data.rows)) throw new Error('Projects query did not return rows');
        await page.locator('#projectsReviewGrid').waitFor({state: 'visible', timeout: 15000});
        // Wait for JET bindings/components, not networkidle (the app has a stream).
        await page.evaluate(async () => {
            if (window.oj?.Context) await window.oj.Context.getPageContext().getBusyContext().whenReady(10000);
        });
        if (failures.length) {
            console.log(JSON.stringify({status: 'failed', failures}));
            return 1;
        }
        console.log(`PASS Projects page loaded and queried successfully, rows=${data.rows.length}`);
        return 0;
    } catch (error) {
        if (!page || /(?:logon|login|signin)/i.test(new URL(page.url()).pathname)) {
            console.log('Smoke environment unavailable: browser or local login state is not ready');
            return 125;
        }
        // Do not print browser state, cookies, query strings, or business values.
        console.log(JSON.stringify({status: 'failed', error: error.name || 'Error', failures}));
        return 1;
    } finally {
        if (browser) await browser.close();
    }
}

if (require.main === module) main().then(code => {process.exitCode = code;});
module.exports = {main};
