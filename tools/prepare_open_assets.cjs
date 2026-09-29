// Publish only reviewed, synthetic UI photographs. Capture tooling stays private.
const fs=require('node:fs'),path=require('node:path');
const {createCanvas,loadImage}=require('@napi-rs/canvas');
const [captures,film]=process.argv.slice(2),root=path.resolve(__dirname,'../web/pai-open');
const frames=['search-result','evidence','quant','decision-note','history','result-filled','teams','dashboard'];
const labels=['공고 검색 결과','문서별 원문 근거','정량 점수와 주의사항','담당자 판단과 사유 입력','최근 3년 낙찰 이력','입찰 결과 입력','Teams 알림 카드 미리보기','오늘 할 일 대시보드'];
(async()=>{
 let html=fs.readFileSync(path.join(root,'index.html'),'utf8');
 for(let i=0;i<frames.length;i++){
  const img=await loadImage(path.join(captures,frames[i]+'.png')),canvas=createCanvas(1600,1000);
  canvas.getContext('2d').drawImage(img,0,0,1600,1000);
  fs.writeFileSync(path.join(root,`assets/pai-screen-${i+1}.webp`),canvas.encodeSync('webp',92));
  const re=new RegExp('(<section class="ps-panel" id="ps-panel-'+i+'"[^>]*>)[\\s\\S]*?</section>');
  if(!re.test(html))throw Error('Missing panel '+i);
  html=html.replace(re,`$1<img class="ps-product-capture" src="/open/assets/pai-screen-${i+1}.webp?v=20260930-glass-1" width="1600" height="1000" loading="lazy" alt="실제 PAI ${labels[i]} 화면. 기능 설명용 예시 데이터."></section>`);
 }
 html=html.replaceAll('20260929-film-1','20260930-glass-1').replaceAll('20260929-film-2','20260930-glass-1').replaceAll('20260929-qhd-2','20260930-glass-1');
 html=html.replace('실제 서비스와 닮은 예시 화면으로 8가지 기능을 살펴보세요.','실제 PAI 화면의 검색·입력·근거 확인 동작으로 8가지 기능을 살펴보세요.').replace('예시 화면으로 보는 PAI 기능','실제 화면으로 보는 PAI 기능').replace('2560 × 1440 · 기능 설명용 예시 화면과 데이터','2560 × 1440 · 실제 PAI 화면 · 예시 데이터');
 fs.writeFileSync(path.join(root,'index.html'),html);
 for(const file of ['pai-product-poster.webp','pai-product-tour.webm'])if(fs.existsSync(path.join(film,file)))fs.copyFileSync(path.join(film,file),path.join(root,'assets',file));
})();
