/* Offline product photography: render the real application, with synthetic data.
 * Nothing in this harness is included in the public application or its bundle.
 * Usage: NODE_PATH=... node tools/capture_open_tour.cjs OUTPUT FONT_DIR
 */
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {chromium} = require('playwright');
const [output, fonts] = process.argv.slice(2);
if (!output || !fonts) throw Error('Expected output and font directories');
fs.mkdirSync(output, {recursive:true});
const root = path.resolve(__dirname, '../src/pai_loop/static');
const fontCSS = [ ['Regular',400], ['SemiBold',600], ['ExtraBold',800] ].map(([name,weight]) => `@font-face{font-family:Paperlogy;src:url('/tour-font-${name}.ttf');font-weight:${weight};font-display:block}`).join('');
const hooks = `
  window.__tour = {state, els, openDetail, closeDetail, selectTab, renderAll, setView,
    openResultLearningDialog, normalizeResultLearningNotice, renderAwardHistoryPanel};
`;
const server = http.createServer((req,res) => {
  const url = new URL(req.url,'http://localhost');
  if(url.pathname.startsWith('/api/')){
    // No API, network, credentials or real records. Only a local synthetic session.
    const payload = url.pathname.endsWith('/accounts/me') ? {enabled:true,authenticated:true,
      account:{id:'SYN-TOUR',role:'DEPARTMENT',department_id:'SYN-DEPT',department_name:'예시 부서',username:'SYN-TOUR'},
      csrf_token:'SYN-TOUR',capabilities:{read_department_records:true,write_results:true,write_decisions:true}} : {};
    res.setHeader('Content-Type','application/json');return res.end(JSON.stringify(payload));
  }
  if(url.pathname.startsWith('/tour-font-')){
    const name=path.basename(url.pathname).replace('tour-font-','Paperlogy-');
    res.setHeader('Content-Type','font/ttf');return res.end(fs.readFileSync(path.join(fonts,name)));
  }
  let name=path.basename(url.pathname);
  if(!name.includes('.')) name='index.html';
  const file=path.join(root,name);
  if(!fs.existsSync(file)){res.writeHead(404);return res.end();}
  let body=fs.readFileSync(file);
  if(name==='index.html') body=Buffer.from(body.toString().replace(/<link\s+rel="stylesheet"\s+href="https:[\s\S]*?\/>/,`<style>${fontCSS}</style>`));
  if(name==='app.js'){
    let js=body.toString().replace(/\}\)\(\);\s*$/,hooks+'\n})();');
    js=js.replace('const payload = createDemoData();',`const payload = createDemoData();
      payload.notices.forEach((n,i)=>{n.notice_number='SYN-OPEN-'+String(i+1).padStart(3,'0');
        n.agency='예시 공공기관';n.demand_agency='예시 사업부서';
        n.analysis_state='EVALUATED';
        if(i>0&&i<4){n.decisions=[{id:'SYN-DECISION-'+i,department_id:'SYN-DEPT',department_name:'예시 부서',choice:'GO',department_revision:1,created_at:new Date().toISOString(),rationale:'예시 검토 의견'}];
          n.deadline=new Date(Date.now()+(i===3?-2:3)*86400000).toISOString();if(i===3)n.status='CLOSED';}
        (n.award_history||[]).forEach(r=>{r.agency='예시 공공기관';r.winner='예시 수행기관';});
        if(i===0){n.title='리더십 교육 운영 용역';n.summary='교육사업의 참가요건과 원문 근거를 함께 검토합니다. 참여인력 증빙은 추가 확인이 필요합니다.';}
      });`);
    body=Buffer.from(js);
  }
  res.setHeader('Content-Type',({'.js':'application/javascript','.css':'text/css','.svg':'image/svg+xml','.png':'image/png'})[path.extname(file)]||'text/html; charset=utf-8');res.end(body);
});
const manifest={viewport:{width:1440,height:900},pixelRatio:2,source:'src/pai_loop/static',synthetic:true,frames:{}};
(async()=>{
  await new Promise(r=>server.listen(8919,'127.0.0.1',r));
  if(process.argv.includes('--serve')){console.log('Offline PAI: http://127.0.0.1:8919/?demo=1');return;}
  const browser=await chromium.launch({channel:'chrome',headless:true});
  const page=await browser.newPage({viewport:manifest.viewport,deviceScaleFactor:2,reducedMotion:'reduce'});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
  async function shot(name, selector, region){
    if(selector) await page.locator(selector).first().scrollIntoViewIfNeeded();
    await page.evaluate(()=>document.fonts.ready);
    const focus=selector?await page.locator(selector).first().boundingBox():null;
    const crop=region?await page.locator(region).boundingBox():null;
    await page.screenshot({path:path.join(output,name+'.png'),animations:'disabled'});
    manifest.frames[name]={file:name+'.png',focus,crop};console.log(name,JSON.stringify(focus));
  }
  try{
    await page.goto('http://127.0.0.1:8919/?demo=1');
    await page.waitForFunction(()=>window.__tour?.state.source==='demo'&&!window.__tour.state.loading);
    await shot('dashboard','.kpi-card--pipeline');
    await page.evaluate(()=>window.__tour.setView('new'));
    await shot('search-empty','#searchInput');
    const word='리더십 교육';
    for(let n=1;n<=word.length;n++){
      await page.locator('#searchInput').fill(word.slice(0,n));
      await shot('search-type-'+n,'#searchInput');
    }
    await page.locator('.notice-search-submit').click();
    await shot('search-result','#noticeTableBody tr td:first-child');
    await page.locator('#noticeTableBody tr').first().click();
    await page.waitForSelector('#detailDrawer.is-open');
    await shot('detail','#detailDrawer','#detailDrawer');
    await page.locator('[data-tab="evidence"]').click();
    await shot('evidence','.evidence-card','.detail-drawer');
    await page.locator('[data-tab="quant"]').click();
    await shot('quant','#scoreOverview','.detail-drawer');
    if(await page.locator('#decisionDockToggle').getAttribute('aria-expanded')!=='true')await page.locator('#decisionDockToggle').click();
    await shot('decision-empty','#decisionForm','.detail-drawer');
    await page.locator('input[name="decision"][value="HOLD"]').check();
    if(!await page.locator('#decisionComment').isVisible())await page.locator('#toggleCommentButton').click();
    const note='참여인력의 최신 증빙을 확인한 뒤 검토하겠습니다.';
    for(let n=1;n<=8;n++){
      await page.locator('#decisionComment').fill(note.slice(0,Math.ceil(note.length*n/8)));
      await shot('decision-type-'+n,'#decisionComment','.detail-drawer');
    }
    await shot('decision-note','#decisionComment','.detail-drawer');
    await page.locator('#decisionDockToggle').click();
    await page.locator('[data-tab="history"]').click();
    await page.evaluate(()=>{
      const q=window.__tour;
      q.state.awardHistoryMeta[q.state.selectedNotice.noticeKey]={status:'demo',intelligence:{annual_award_table:{
        years:[2025,2024,2023],match_basis:'SIMILAR_CANDIDATES_ONLY',rows:[2025,2024,2023].map((year,i)=>({year,result_group_key:'SYN-AWARD-'+year,project_title:'리더십 역량강화 교육 운영 · 예시',agency:'예시 공공기관',company_name:'예시 수행기관',bid_notice_no:'SYN-AWARD-'+year,revision_no:'00',event_date:year+'-08-20',match_kind:'SIMILAR_PROJECT',participation_kind:'WINNER',bid_amount:120000000+i*10000000,technical_evaluation:70+i,price_evaluation:18,total_evaluation:88+i,source_kind:'SYNTHETIC'}))
      }}};q.renderAwardHistoryPanel(q.state.selectedNotice);
    });
    await shot('history','.history-award-project','.detail-drawer');
    await page.locator('[data-tab="teams"]').click();
    await shot('teams','.teams-window','.teams-window');
    await page.evaluate(()=>{const q=window.__tour;q.closeDetail();q.state.source='api';q.openResultLearningDialog(q.normalizeResultLearningNotice({notice_key:'SYN-OPEN-001',bid_notice_no:'SYN-OPEN-001',revision_no:'00',title:'리더십 교육 운영 용역',agency:'예시 공공기관',outcomes:[]}));});
    await shot('result-empty','#resultLearningStatus','#resultLearningDialog');
    await page.locator('#resultLearningStatus').selectOption('WON');
    await page.locator('#resultLearningSourceReference').fill('공식 개찰 결과 확인 · 예시');
    await shot('result-filled','#resultLearningSourceReference','#resultLearningDialog');
    if(errors.length) throw Error(errors.join('\n'));
    fs.writeFileSync(path.join(output,'captures.json'),JSON.stringify(manifest,null,2));
  }finally{await browser.close();server.close();}
})().catch(e=>{console.error(e);server.close();process.exit(1)});
