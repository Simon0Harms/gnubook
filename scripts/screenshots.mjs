// Regenerate the README screenshots from the synthetic demo book (never use real data!).
//   gnubook demo-book /tmp/demo.gnucash   # config pointing to it, user "demo" / "demo-passwort-123"
//   gunicorn ... &  then:  node scripts/screenshots.mjs http://127.0.0.1:8765 docs/screenshots
// Needs Node with the "playwright" package and a Chromium browser.
import { chromium } from 'playwright';

const base = process.argv[2] || 'http://127.0.0.1:8765';
const out = process.argv[3] || 'docs/screenshots';
const user = process.env.GNUBOOK_USER || 'demo';
const password = process.env.GNUBOOK_PASSWORD || 'demo-passwort-123';

const browser = await chromium.launch(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {});
async function session(viewport) {
  const ctx = await browser.newContext({ viewport, locale: 'de-DE', colorScheme: 'light' });
  const page = await ctx.newPage();
  await page.goto(base + '/login');
  await page.fill('#username', user);
  await page.fill('#password', password);
  await Promise.all([page.waitForNavigation(), page.click('button[type=submit]')]);
  return { ctx, page };
}
async function registerUrl(page, name) {
  await page.goto(base + '/accounts');
  return base + await page.locator('#account-tree a', { hasText: name }).first().getAttribute('href');
}

const { ctx, page } = await session({ width: 1360, height: 860 });
await page.goto(base + '/');
await page.screenshot({ path: `${out}/dashboard.png` });
const giro = await registerUrl(page, 'Girokonto Musterbank');
await page.goto(giro);
await page.screenshot({ path: `${out}/register.png` });
await page.click('.app-content-header a.btn-success');
await page.waitForSelector('#tx-form');
await page.fill('#description', 'KARTENZAHLUNG REWE Wocheneinkauf');
await page.click('#add-row');
// fill the rows through the DOM / Tom Select API (typing into Tom Select is timing-sensitive)
await page.evaluate(() => {
  const rows = document.querySelectorAll('tr.split-row');
  const pick = (row, label) => {
    const sel = row.querySelector('select');
    const opt = Array.from(sel.options).find(o => o.text.endsWith(label));
    sel.tomselect.setValue(opt.value);
  };
  const set = (row, name, value) => {
    const el = row.querySelector(`[name$="-${name}"]`);
    el.value = value;
    el.dispatchEvent(new Event('input', { bubbles: true }));
  };
  set(rows[0], 'credit', '48,37');
  pick(rows[1], 'Lebensmittel'); set(rows[1], 'debit', '39,88');
  pick(rows[2], 'Haushalt'); set(rows[2], 'memo', 'Spülmittel, Küchenrolle');
});
await page.click('#balance-btn');
await page.screenshot({ path: `${out}/split-form.png` });
await page.goto(base + '/checkpoints?all=1');
await page.screenshot({ path: `${out}/checkpoints.png` });
await page.goto(base + '/imports?all=1');
await page.screenshot({ path: `${out}/imports.png` });
await ctx.close();

const mobile = await session({ width: 390, height: 844 });
await mobile.page.goto(giro);
await mobile.page.screenshot({ path: `${out}/mobile-register.png` });
await mobile.ctx.close();
await browser.close();
