/* Run against node web/pai-open/preview.mjs. Viewport, motion and keyboard QA. */
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const out=path.resolve(process.argv[2]||'.local/open-qa');fs.mkdirSync(out,{recursive:true});
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const report=[];
 try{
  for(const [width,height,motion]of [[1440,900,'no-preference'],[1280,720,'no-preference'],[1024,768,'no-preference'],[901,650,'no-preference'],[1440,900,'reduce'],[768,900,'reduce'],[390,844,'no-preference'],[320,760,'reduce']]){
   await page.setViewportSize({width,height});await page.emulateMedia({reducedMotion:motion});
   await page.goto('http://127.0.0.1:8788/open');await page.evaluate(()=>document.fonts.ready);
   await page.locator('#features').scrollIntoViewIfNeeded();
   const enhanced=width>=901&&height>=650;
   assert.equal(await page.locator('.ps-runway.is-enhanced').count(),enhanced?1:0);
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),`overflow ${width}`);
   if(enhanced){
    for(let i=0;i<8;i++){
     await page.locator(`[data-ps-go="${i}"]`).click();
     await page.waitForFunction(n=>{const e=document.getElementById('ps-copy-'+n);return e&&getComputedStyle(e).opacity==='1';},i);
     const geometry=await page.evaluate(i=>{
      const rect=e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,bottom:r.bottom,right:r.right}};
      const copy=document.getElementById('ps-copy-'+i),window=document.querySelector('.ps-copy-window'),stage=document.querySelector('.ps-pinned'),link=copy.querySelector('.ps-cta');
      return {copy:rect(copy),window:rect(window),stage:rect(stage),link:rect(link),enabled:!link.inert&&link.tabIndex!==-1,panel:rect(document.getElementById('ps-panel-'+i))};
     },i);
     assert.ok(geometry.enabled,`CTA disabled ${width} ${i}`);
     assert.ok(geometry.copy.y>=geometry.window.y-2&&geometry.copy.bottom<=geometry.window.bottom+2,`copy clipped ${width}x${height} scene ${i+1}: ${JSON.stringify(geometry)}`);
     assert.ok(Math.abs(geometry.stage.y-88)<3,`sticky failed ${width} scene ${i+1}: ${geometry.stage.y}`);
     if(width===1440&&motion==='no-preference')await page.screenshot({path:path.join(out,`story-${i+1}.png`)});
    }
    await page.locator('[data-ps-go="7"]').press('Home');
    await page.waitForFunction(()=>document.querySelector('[data-pai-story]').dataset.activeStep==='1');
    assert.equal(await page.locator('[data-ps-go="0"]').evaluate(e=>e===document.activeElement),true);
   }
   await page.screenshot({path:path.join(out,`page-${width}-${motion}.png`)});
   report.push({width,height,motion,enhanced,pass:true});
  }
  await page.setViewportSize({width:1440,height:900});
  await page.emulateMedia({reducedMotion:'reduce'});await page.goto('http://127.0.0.1:8788/open');
  assert.equal(await page.locator('#heroVideo').evaluate(e=>e.paused),true);
  await page.locator('[data-ps-motion]').click();assert.equal(await page.locator('#features').getAttribute('data-motion-state'),'on');
  await page.locator('[data-ps-motion]').click();assert.equal(await page.locator('#features').getAttribute('data-motion-state'),'off');
  const nojs=await browser.newPage({javaScriptEnabled:false,viewport:{width:1440,height:900}});await nojs.goto('http://127.0.0.1:8788/open');
  assert.equal(await nojs.locator('.ps-step').count(),8);assert.equal(await nojs.locator('.ps-product-capture').count(),8);await nojs.close();
  assert.deepEqual(errors,[]);fs.writeFileSync(path.join(out,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
