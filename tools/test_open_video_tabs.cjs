/* Run against the public preview server; no application data is accessed. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const out = path.resolve(process.argv[2] || '.local/video-qa');
const url = process.env.PAI_OPEN_PREVIEW_URL || 'http://127.0.0.1:8788/open';
fs.mkdirSync(out, {recursive: true});
(async () => {
  const browser = await chromium.launch({channel: 'chrome', headless: true});
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const report = [];
  const checkSelected = async index => {
    assert.deepEqual(await page.locator('.film-tab').evaluateAll(tabs => tabs.map(t => t.getAttribute('aria-selected'))), index === 0 ? ['true', 'false'] : ['false', 'true']);
    assert.deepEqual(await page.locator('.film-tab').evaluateAll(tabs => tabs.map(t => t.tabIndex)), index === 0 ? [0, -1] : [-1, 0]);
    assert.deepEqual(await page.locator('[data-film-panel]').evaluateAll(panels => panels.map(p => p.hidden)), index === 0 ? [false, true] : [true, false]);
    assert.equal(await page.locator(index === 0 ? '#tourVideo' : '#heroVideo').evaluate(video => video.paused), true);
  };
  try {
    await page.setViewportSize({width: 1440, height: 900});
    await page.goto(url);
    await checkSelected(0);
    await page.waitForFunction(() => document.getElementById('heroVideo').currentTime > 0.1);
    assert.equal(await page.locator('#heroVideo').evaluate(v => v.muted), true);
    assert.equal(await page.locator('#tourVideo').evaluate(v => v.readyState), 0);
    const ad = await page.locator('#heroVideo').evaluate(v => ({duration:v.duration,width:v.videoWidth,height:v.videoHeight}));
    assert.ok(Math.abs(ad.duration - 99.4) < 0.1);
    assert.equal(ad.width, 1920);
    assert.equal(ad.height, 1080);
    await page.locator('#filmTabTour').click();
    await checkSelected(1);
    await page.waitForFunction(() => document.getElementById('tourVideo').currentTime > 0.1);
    const tour = await page.locator('#tourVideo').evaluate(v => ({duration:v.duration,width:v.videoWidth,height:v.videoHeight}));
    assert.ok(Math.abs(tour.duration - 60) < 0.1);
    assert.equal(tour.width, 2560);
    await page.locator('#filmTabTour').press('ArrowRight');
    await checkSelected(0);
    assert.equal(await page.locator('#filmTabAd').evaluate(e => e === document.activeElement), true);
    await page.locator('#filmTabAd').press('End');
    await checkSelected(1);
    await page.locator('#filmTabTour').press('Home');
    await checkSelected(0);
    await page.locator('#filmTabAd').press('ArrowLeft');
    await checkSelected(1);
    await page.reload();
    await checkSelected(0);
    report.push({playback: {ad, tour}, keyboard: true, inactivePaused: true, reloadDefaultsToAd: true});
    for (const width of [1440, 768, 390, 320]) {
      await page.setViewportSize({width, height: 900});
      await page.emulateMedia({reducedMotion:'reduce'});
      await page.goto(url);
      await checkSelected(0);
      assert.equal(await page.locator('#heroVideo').evaluate(v => v.paused), true);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `overflow at ${width}`);
      await page.screenshot({path:path.join(out, `ad-${width}.png`)});
      await page.locator('#filmTabTour').click();
      await checkSelected(1);
      assert.equal(await page.locator('#tourVideo').evaluate(v => v.paused), true);
      await page.screenshot({path:path.join(out, `tour-${width}.png`)});
      report.push({width, reducedMotion:true, pass:true});
    }
    const nojs = await browser.newPage({javaScriptEnabled:false});
    await nojs.goto(url);
    assert.equal(await nojs.locator('.film-tabs').isVisible(), false);
    assert.equal(await nojs.locator('#heroVideo').isVisible(), true);
    assert.equal(await nojs.locator('#tourVideo').isVisible(), true);
    await nojs.close();
    assert.deepEqual(errors, []);
    report.push({noJavaScriptBothFilms:true, errors});
    fs.writeFileSync(path.join(out, 'report.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report));
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exit(1);});
