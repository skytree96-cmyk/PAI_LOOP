/* Reproducible synthetic PAI product film. No browser, API, or private data inputs.
 * NODE_PATH must resolve @napi-rs/canvas. Arguments: output-dir font-dir ffmpeg [--stills]
 */
const {createCanvas,GlobalFonts}=require('@napi-rs/canvas');
const fs=require('node:fs'); const path=require('node:path'); const {spawn}=require('node:child_process'); const {once}=require('node:events');
const [out,fontDir,ffmpeg,...flags]=process.argv.slice(2);
if(!out||!fontDir) throw Error('Usage: render_open_tour.cjs output-dir font-dir ffmpeg [--stills]');
fs.mkdirSync(out,{recursive:true});
for(const [file,alias] of [['Paperlogy-4Regular.ttf','Paper'],['Paperlogy-6SemiBold.ttf','PaperSemi'],['Paperlogy-7Bold.ttf','PaperBold']]){
 if(!GlobalFonts.registerFromPath(path.join(fontDir,file),alias))throw Error('Missing font: '+file);
}
const W=2560,H=1440,FPS=30,DURATION=60;
const C={nav:'#0f4c81',ink:'#183a55',muted:'#708597',line:'#d9e3eb',cream:'#f0eee9',pale:'#edf5fb',good:'#2c7b67',green:'#e8f4ee',amber:'#8b651f',yellow:'#fff2d7',purple:'#6264a7',white:'#ffffff'};
let ctx;
function box(x,y,w,h,r=12,fill=C.white,stroke=null){ctx.beginPath();ctx.roundRect(x,y,w,h,r);if(fill){ctx.fillStyle=fill;ctx.fill()}if(stroke){ctx.strokeStyle=stroke;ctx.lineWidth=1;ctx.stroke()}}
function text(s,x,y,size=16,color=C.ink,font='Paper',align='left'){ctx.font=`${size}px ${font}`;ctx.textAlign=align;ctx.textBaseline='top';ctx.fillStyle=color;ctx.fillText(s,x,y)}
function line(x1,y1,x2,y2,color=C.line,width=1){ctx.beginPath();ctx.moveTo(x1,y1);ctx.lineTo(x2,y2);ctx.strokeStyle=color;ctx.lineWidth=width;ctx.stroke()}
function pill(s,x,y,w=90,fill=C.pale,color=C.nav){box(x,y,w,28,7,fill);text(s,x+w/2,y+6,13,color,'PaperSemi','center')}
function title(s,x,y){text(s,x,y,19,C.ink,'PaperSemi')}
function label(s,x,y){text(s,x,y,13,C.muted)}
function value(s,x,y){text(s,x,y,17,C.ink,'PaperSemi')}
function card(x,y,w,h,heading){box(x,y,w,h,13,C.white,C.line);if(heading)title(heading,x+20,y+20)}
function input(s,x,y,w=300){box(x,y,w,37,7,'#f9fbfd',C.line);text(s,x+13,y+10,14)}
function metric(x,y,w,n,s){card(x,y,w,84);label(s,x+17,y+14);text(n,x+17,y+37,30,C.nav,'PaperBold');text('건',x+65,y+51,13,C.muted)}
function app(active){
 ctx.save();ctx.shadowColor='#123e5720';ctx.shadowBlur=22;ctx.shadowOffsetY=8;box(55,175,1170,438,18,C.cream);ctx.restore();
 ctx.save();ctx.beginPath();ctx.roundRect(55,175,1170,438,18);ctx.clip();
 const g=ctx.createLinearGradient(55,175,1225,245);g.addColorStop(0,'#1c659e');g.addColorStop(1,'#103c63');box(55,175,1170,57,0,g);
 text('PAI',80,188,30,C.white,'PaperBold');text('.',134,187,30,'#70c2e6','PaperBold');
 ['대시보드','공고 탐색','검토 관리','결과·실적'].forEach((s,i)=>{let x=196+i*137;if(i===active){box(x-10,175,118,57,0,'#ffffff12');line(x,229,x+88,229,'#b9e3f6',3)}text(s,x,197,15,i===active?'#ffffff':'#d0e0ec','PaperSemi')});
 text('예시 부서',1090,197,14,'#d0e0ec');ctx.restore();
}
const scenes=[
 {name:'탐색',title:'AI로 검토할 공고 찾기',sub:'공고·사전규격 검색부터 검토 대상 선택까지',active:1,captions:['공고명·기관·공고번호로 필요한 공고를 찾으세요.','부서와 관심 분야를 좁혀, 검토할 공고를 선택합니다.'],focus:[[83,258,756,43],[82,379,1116,61]],draw(){
  input('',83,258,756);pill('검색',856,262,100,C.nav,C.white);pill('사전규격',976,262,115);
  ['교육·연수','용역','접수 중'].forEach((s,i)=>pill(s,84+i*118,315,105,i===0?C.pale:'#ffffff'));
  box(82,365,1116,220,10,C.white,C.line);label('공고명 / 기관',103,347);label('마감',864,347);label('참가자격',1013,347);
  [['리더십 교육 운영 용역','예시 공공기관 · SYN-OPEN-001','D−5','확인 필요'],['지역기업 역량강화 아카데미','예시 지원기관 · SYN-OPEN-002','D−8','충족'],['공공부문 교육과정 개발','예시 교육기관 · SYN-OPEN-003','D−12','분석 전']].forEach((r,i)=>{let y=379+i*68;if(i===0)box(83,y-3,1114,65,7,'#edf5fb');value(r[0],104,y+5);label(r[1],104,y+31);pill(r[2],851,y+13,75);pill(r[3],1000,y+13,103,i===0?C.yellow:i===1?C.green:C.pale,i===0?C.amber:i===1?C.good:C.muted);if(i<2)line(103,y+63,1176,y+63)})
 }},
 {name:'문서 분석',title:'AI가 문서 속 조건을 정리',sub:'참가요건 · 정량평가 기준 · 원문 근거',active:2,captions:['AI가 공고문과 제안요청서의 조건을 정리합니다.','요약 옆의 원문 위치를 확인하고, 첨부별 처리 상태를 살펴보세요.'],focus:[[104,383,466,63],[630,297,568,84]],draw(){
  pill('공고문 · 분석 완료',82,249,190,C.green,C.good);pill('제안요청서 · 분석 완료',285,249,223,C.green,C.good);label('리더십 교육 운영 용역',899,257);
  card(82,294,511,294);box(83,295,509,40,12,'#eef3f7');label('제안요청서.pdf',103,308);label('17 / 48',501,308);
  title('Ⅲ. 입찰 참가자격',106,347);box(104,383,466,63,7,'#e4f0fb');text('유사 교육사업 수행실적을',119,393,17,C.nav,'PaperSemi');text('증명하는 자료 제출',119,418,17,C.nav,'PaperSemi');
  for(let i=0;i<4;i++)line(109,465+i*21,542-(i%2)*71,465+i*21,'#dce4ea',4);label('원문 17쪽',470,560);
  [['참가요건','유사사업 수행실적 증빙','17쪽'],['정량평가 기준','실적·인력의 평가 배점','28쪽'],['제출 조건','제안서·발표자료·증빙','35쪽']].forEach((r,i)=>{let y=297+i*94;card(630,y,568,84);title(r[0],650,y+13);text(r[1],650,y+44,15,C.muted);pill(r[2]+'  ↗',1080,y+27,98)})
 }},
 {name:'회사 비교',title:'추출한 조건을 회사정보와 비교',sub:'참가자격과 정량 점수, 추가 확인 사항을 구분',active:2,captions:['충족한 조건과 확인할 증빙을 나란히 비교합니다.','정량 점수는 미산정·예상·확정을 구분해 확인하세요.'],focus:[[100,438,647,43],[782,294,416,190]],draw(){
  title('리더십 교육 운영 용역',83,253);pill('추가 증빙 확인',1035,248,165,C.yellow,C.amber);
  card(82,294,680,294,'참가자격 비교');label('공고 조건',105,339);label('회사정보 / 증빙',394,339);
  [['유사 교육사업 수행실적','실적 증빙 확인','충족'],['참여인력 재직 증빙','최신 자료 추가 확인','확인 필요'],['제출서류 유효기간','공고 마감일 기준 검토','충족']].forEach((r,i)=>{let y=374+i*65;line(104,y-10,741,y-10);text(r[0],106,y+9,16);text(r[1],396,y+9,14,C.muted);pill(r[2],640,y+2,96,i===1?C.yellow:C.green,i===1?C.amber:C.good)});
  card(782,294,416,190,'정량 점수 · 예상');text('15–17',802,343,49,C.nav,'PaperBold');text('/ 20점',993,374,19,C.muted);box(802,417,370,7,4,'#e1ebf2');box(802,417,294,7,4,'#75a8d0');text('예시 자료 기준 · 확정 점수가 아닙니다.',802,445,14,C.muted);
  box(782,500,416,88,12,C.yellow);text('참여인력 증빙 1건 확인 필요',802,518,17,C.amber,'PaperSemi');text('제출 준비도와 리스크는 별도로 검토합니다.',802,548,14,C.amber)
 }},
 {name:'담당자 판단',title:'AI 검토를 바탕으로 담당자가 결정',sub:'참여 여부와 판단 이유를 함께 기록',active:2,captions:['분석 결과와 확인 사항을 살펴보고 참여·보류·불참을 결정합니다.','판단 이유를 남기면 다음 검토에서도 맥락을 이어갈 수 있습니다.'],focus:[[103,336,651,40],[103,415,651,73]],draw(){
  card(82,254,694,336,'리더십 교육 운영 용역');label('참여 여부',104,307);
  [['참여',C.white,C.muted],['보류',C.yellow,C.amber],['불참',C.white,C.muted]].forEach((r,i)=>{let x=103+i*222;box(x,336,208,40,8,r[1],i===1?'#dcc18e':C.line);text(r[0],x+104,347,17,r[2],'PaperSemi','center')});
  label('판단 사유',104,391);box(103,415,651,73,9,'#f7f9fb',C.line);text('참여인력의 최신 증빙을 확인한 뒤',119,429,17);text('참여 여부를 다시 검토하겠습니다.',119,453,17);
  label('예시 부서 · 검토 의견',105,547);pill('판단 저장',608,535,147,C.nav,C.white);
  card(796,254,402,336,'검토 기록');pill('보류',817,305,84,C.yellow,C.amber);text('추가 증빙 확인 예정',818,354,21,C.ink,'PaperSemi');line(822,407,822,524,'#bed6e8',2);
  [['문서 분석','참가요건·근거 확인'],['담당자 판단','판단 사유 기록'],['후속 검토','증빙 확인 후 재검토']].forEach((r,i)=>{let y=403+i*53;box(817,y,10,10,5,i===1?C.nav:'#93b8d3');text(r[0],843,y-2,15,C.ink,'PaperSemi');text(r[1],843,y+20,13,C.muted)})
 }},
 {name:'참고자료',title:'과거 낙찰과 수행실적으로 검토 보완',sub:'유사사업의 가격 정보와 우리 회사 수행실적',active:3,captions:['유사 사업의 낙찰 이력으로 검토에 필요한 맥락을 더합니다.','수행실적은 이번 공고의 인정 기간·과업 범위·증빙 조건과 비교하세요.'],focus:[[93,347,530,92],[663,347,527,92]],draw(){
  pill('최근 3년',82,248,115);pill('교육·연수',209,248,122);pill('예시 부서',343,248,120);
  card(82,295,548,293,'유사 사업 낙찰 이력');card(650,295,548,293,'우리 회사 수행실적');
  [['공공기관 교육 운영','교육·연수 · 유사 과업','공식 결과 출처 확인'],['리더십 아카데미','교육 기획·운영 · 유사 과업','가격 정보 참고']].forEach((r,i)=>{let y=352+i*108;line(103,y-10,607,y-10);value(r[0],105,y+2);label(r[1],105,y+31);pill(r[2],105,y+57,186)});
  [['리더십 역량강화 과정','예시 실적 · 완료','기간·과업 범위 확인'],['공공부문 교육사업','예시 실적 · 완료','증빙 자료 확인']].forEach((r,i)=>{let y=352+i*108;line(671,y-10,1175,y-10);value(r[0],673,y+2);label(r[1],673,y+31);pill(r[2],673,y+57,186,C.green,C.good)})
 }},
 {name:'결과 기록',title:'입찰 결과를 다음 검토의 자료로',sub:'수주·패찰 결과와 금액·사유·출처를 기록',active:3,captions:['확인된 입찰 결과와 금액, 점수, 사유를 기록합니다.','결과의 출처와 확인 상태를 함께 남겨 다음 검토에 활용하세요.'],focus:[[103,334,651,37],[816,339,362,117]],draw(){
  card(82,254,694,335,'리더십 교육 운영 용역');label('입찰 결과',103,310);pill('낙찰 · 예시',103,334,150,C.green,C.good);label('낙찰 금액',377,310);input('120,000,000 원',377,334,370);
  label('결과 메모',103,391);box(103,415,651,75,8,'#f7f9fb',C.line);text('공식 개찰 결과를 확인하고 기록했습니다.',119,435,17);text('금액·점수·사유와 관련 출처를 함께 보관합니다.',119,461,14,C.muted);pill('결과 저장',607,535,147,C.nav,C.white);
  card(796,254,402,335,'출처 및 확인 상태');label('결과 출처',816,313);input('공식 결과 공고',816,339,362);label('확인 상태',816,402);pill('출처 확인',816,428,124,C.green,C.good);line(816,479,1178,479);text('검토 → 담당자 판단 → 입찰 결과',816,506,16,C.nav,'PaperSemi');label('같은 공고에 기록을 이어갑니다.',816,543)
 }},
 {name:'Teams 알림',title:'MS Teams 맞춤 알림 연동',sub:'관심 공고 일정 리마인드 · 부서 맞춤 신규 공고',active:1,captions:['관심 공고를 등록하면 마감 리마인드를 Teams로 받을 수 있습니다.','부서 맞춤 데일리 브리핑으로 새로운 공고도 놓치지 마세요.'],focus:[[103,349,212,28],[541,324,637,246]],draw(){
  card(82,254,418,335,'관심 공고 설정');text('리더십 교육 운영 용역',103,310,18,C.ink,'PaperSemi');pill('관심 공고 등록',103,349,212,C.yellow,C.amber);line(102,402,479,402);title('내 부서 데일리 브리핑',104,426);label('예시 부서 · 교육·연수 분야',104,460);box(420,423,57,28,14,C.nav);box(451,427,20,20,10,C.white);text('Teams 개인 채팅으로 수신',104,521,15,C.nav,'PaperSemi');label('관심 공고와 브리핑 수신을 각각 관리',104,553);
  box(521,254,677,335,14,'#f4f3fa',C.line);box(541,274,31,31,8,C.purple);text('T',550,280,20,C.white,'PaperBold');text('PAI · Teams 개인 채팅',584,281,18,C.ink,'PaperSemi');
  [[541,'FOLLOWED NOTICE','관심 공고 마감 알림','리더십 교육 운영 용역','마감 5일 전','공고 상세 보기'],[871,'DAILY BRIEFING','부서 맞춤 신규 공고','내 부서 키워드 기준','오늘 추천 공고 3건','공고 목록 보기']].forEach(r=>{let x=r[0];card(x,324,307,246);box(x+1,324,305,4,2,C.purple);text(r[1],x+17,345,11,C.purple,'PaperSemi');title(r[2],x+17,373);text(r[3],x+17,410,14,C.muted);text(r[4],x+17,445,23,C.nav,'PaperBold');label('자격·마감일·확인 사항 요약',x+17,486);pill(r[5]+'  ↗',x+17,526,271)})
 }},
 {name:'업무 관리',title:'분석 현황과 검토 우선순위',sub:'오늘 확인할 일을 먼저, 입찰 이후의 기록까지',active:0,captions:['담당자 판단이 필요한 공고를 먼저 확인하세요.','참여 공고의 5일 내 마감 일정과 개찰 후 결과 입력까지 챙깁니다.'],focus:[[82,351,358,238],[461,351,737,238]],draw(){
  metric(82,250,358,'12','판단 필요');metric(461,250,358,'5','5일 내 마감');metric(840,250,358,'3','결과 입력');
  const cols=[{x:82,title:'입찰참가여부 결정',badge:'우선 검토',color:C.nav,fill:C.pale,rows:['부서 키워드 매칭 공고','참가자격 충족 공고'],foot:'미결정 공고부터 검토'}, {x:461,title:'참여 공고 진행 현황',badge:'D−5',color:C.amber,fill:C.yellow,rows:['제안서 작성 중','제출 준비 사항 확인'],foot:'5일 이내 마감 공고 집중 관리'}, {x:840,title:'개찰 완료 결과 기록',badge:'결과 입력',color:C.good,fill:C.green,rows:['수주·패찰 결과 확인','최종 투찰가·출처 기록'],foot:'이번 결과를 다음 검토에 활용'}];
  cols.forEach(o=>{card(o.x,351,358,238,o.title);pill(o.badge,o.x+20,398,112,o.fill,o.color);o.rows.forEach((r,i)=>{box(o.x+22,451+i*34,7,7,3,o.color);text(r,o.x+42,445+i*34,16)});line(o.x+20,527,o.x+337,527);text(o.foot,o.x+20,548,14,C.muted)})
 }}
];
function header(i){
 ctx.fillStyle='#f7f9fb';ctx.fillRect(0,0,1280,720);
 let g=ctx.createLinearGradient(0,0,1280,550);g.addColorStop(0,'#f7fbfe');g.addColorStop(1,'#edf2f5');ctx.fillStyle=g;ctx.fillRect(0,0,1280,720);
 pill(String(i+1).padStart(2,'0')+' / 08',57,34,89,C.nav,C.white);text(scenes[i].name,160,39,16,C.nav,'PaperSemi');text('PAI  /  PRODUCT TOUR',1221,42,13,C.muted,'PaperSemi','right');
 text(scenes[i].title,55,81,34,C.ink,'PaperBold');text(scenes[i].sub,57,130,17,C.muted);
 app(scenes[i].active);scenes[i].draw();
}
const bases=scenes.map((s,i)=>{let c=createCanvas(W,H);ctx=c.getContext('2d');ctx.scale(2,2);header(i);return c});
function ease(n){n=Math.max(0,Math.min(1,n));return n*n*(3-2*n)}
function focus(rect,amount,time){if(amount<=0)return;ctx.save();ctx.globalAlpha=amount;const [x,y,w,h]=rect;ctx.shadowColor='#4294d550';ctx.shadowBlur=12;ctx.lineWidth=2.2;ctx.strokeStyle='#438dc8';ctx.beginPath();ctx.roundRect(x-4,y-4,w+8,h+8,11);ctx.stroke();ctx.restore();
 ctx.save();ctx.translate(rect[0]+rect[2]-13,rect[1]+rect[3]-6);ctx.globalAlpha=amount;ctx.fillStyle=C.nav;ctx.beginPath();ctx.moveTo(0,0);ctx.lineTo(5,21);ctx.lineTo(11,14);ctx.lineTo(21,11);ctx.closePath();ctx.fill();ctx.strokeStyle='white';ctx.lineWidth=1.5;ctx.stroke();ctx.restore();
}
function progress(i,p){for(let j=0;j<8;j++){let x=57+j*148;box(x,702,137,4,2,'#d4e2ed');if(j<=i)box(x,702,137*(j<i?1:p),4,2,C.nav)}}
function frameScene(target,i,local){
 ctx=target.getContext('2d');ctx.save();ctx.scale(2,2);ctx.drawImage(bases[i],0,0,1280,720);
 const phase=local<3.4?0:1;const alpha=ease((local-(phase?3.4:0.6))/.45)*ease((6.75-local)/.35);
 focus(scenes[i].focus[phase],alpha,local);
 if(i===0){box(84,259,752,40,7,'#f9fbfd');text('리더십 교육'.slice(0,Math.min(6,Math.floor(local*6))),99,270,17,C.ink,'PaperSemi');ctx.beginPath();ctx.arc(815,277,7,0,Math.PI*2);ctx.strokeStyle=C.muted;ctx.lineWidth=2;ctx.stroke();line(820,282,826,288,C.muted,2)}
 if(i===3&&local>4.5){let a=ease((local-4.5)/.4);ctx.save();ctx.globalAlpha=a;box(862,546,312,30,8,C.green);text('판단과 이유가 저장되었습니다.',875,554,13,C.good,'PaperSemi');ctx.restore()}
 if(i===5&&local>4.5){let a=ease((local-4.5)/.4);ctx.save();ctx.globalAlpha=a;box(870,545,310,31,8,C.green);text('출처와 함께 결과 기록 완료',883,554,13,C.good,'PaperSemi');ctx.restore()}
 box(55,632,1170,51,12,'#163e60');text(scenes[i].captions[phase],640,648,20,C.white,'PaperSemi','center');
 text('PAI 기능 설명용 예시 · 실제 기관·공고·평가 결과가 아닙니다.',1219,615,10,C.muted,'Paper','right');progress(i,local/6.75);ctx.restore();
}
function bookend(target,end,t){ctx=target.getContext('2d');ctx.save();ctx.scale(2,2);let g=ctx.createLinearGradient(0,0,1280,720);g.addColorStop(0,'#0b2e4a');g.addColorStop(1,'#155783');ctx.fillStyle=g;ctx.fillRect(0,0,1280,720);
 ctx.save();ctx.globalAlpha=.12;for(let i=0;i<4;i++){ctx.beginPath();ctx.arc(1160,80,190+i*95,0,Math.PI*2);ctx.strokeStyle='#a5dcf2';ctx.lineWidth=1;ctx.stroke()}ctx.restore();
 text('PAI',70,65,38,C.white,'PaperBold');text('.',133,64,38,'#82d0ed','PaperBold');text('AI 공공입찰 워크스페이스',159,80,16,'#c0d9ec');
 text(end?'다음 공고,':'공고를 찾고,',70,205,60,C.white,'PaperBold');text(end?'근거와 함께 검토하세요.':'근거를 확인하고, 판단합니다.',70,284,52,C.white,'PaperBold');
 text(end?'판단·알림·결과까지, PAI에서 하나의 흐름으로.':'탐색부터 문서 분석, Teams 알림과 업무 관리까지.',74,382,23,'#c2ddec');
 if(end){box(74,469,350,64,14,C.white);text('pai.kma.or.kr',249,488,25,C.nav,'PaperSemi','center');text('AI는 조건과 근거를 정리하고, 최종 참여는 담당자가 결정합니다.',74,580,18,'#bed4e5')}
 else{scenes.forEach((s,i)=>{let x=74+(i%4)*280,y=470+Math.floor(i/4)*65;box(x,y,259,48,10,'#ffffff0d','#ffffff22');text(String(i+1).padStart(2,'0'),x+16,y+14,15,'#83c7e6','PaperSemi');text(s.name,x+55,y+13,18,'#ecf6fd','PaperSemi')});text('60초 기능 소개  ·  QHD  ·  예시 데이터',74,640,13,'#9ebed5')}
 ctx.restore();
}
const a=createCanvas(W,H),b=createCanvas(W,H),final=createCanvas(W,H);
function render(time){
 if(time<3){bookend(final,false,time);return final}
 if(time>=57){bookend(final,true,time-57);return final}
 let i=Math.min(7,Math.floor((time-3)/6.75)),local=(time-3)-i*6.75;
 frameScene(final,i,local);
 if(local<.32){if(i===0)bookend(a,false,3);else frameScene(a,i-1,6.74);const f=final.getContext('2d');f.save();f.globalAlpha=1-ease(local/.32);f.drawImage(a,0,0);f.restore()}
 return final;
}
(async()=>{
 for(let i=0;i<8;i++){const c=render(3+i*6.75+4.3);fs.writeFileSync(path.join(out,`scene-${i+1}.png`),c.encodeSync('png'))}
 bookend(final,false,2);fs.writeFileSync(path.join(out,'poster.png'),final.encodeSync('png'));fs.writeFileSync(path.join(out,'pai-product-poster.webp'),final.encodeSync('webp',95));
 fs.writeFileSync(path.join(out,'tour-manifest.json'),JSON.stringify({width:W,height:H,fps:FPS,duration:DURATION,synthetic:true,chapters:scenes.map((s,i)=>({start:3+i*6.75,title:s.name,caption:s.captions}))},null,2));
 if(flags.includes('--stills'))return;
 if(!ffmpeg)throw Error('FFmpeg path required');
 const log=fs.openSync(path.join(out,'encode.log'),'w');
 const args=['-y','-f','rawvideo','-pix_fmt','rgba','-s',`${W}x${H}`,'-r',String(FPS),'-i','pipe:0','-an','-c:v','libx264','-preset','fast','-crf','17','-pix_fmt','yuv420p','-movflags','+faststart',path.join(out,'pai-product-tour-qhd.mp4'),'-an','-c:v','libvpx-vp9','-b:v','0','-crf','27','-cpu-used','5','-row-mt','1','-threads','6','-pix_fmt','yuv420p',path.join(out,'pai-product-tour.webm')];
 const child=spawn(ffmpeg,args,{stdio:['pipe','ignore',log],windowsHide:true});
 child.stdin.on('error',e=>{throw e});
 for(let n=0;n<FPS*DURATION;n++){let c=render(n/FPS);if(!child.stdin.write(c.data()))await once(child.stdin,'drain');if(n%(FPS*5)===0)console.log(`render ${n/FPS}/${DURATION}s`)}
 child.stdin.end();const [code]=await once(child,'close');fs.closeSync(log);if(code!==0)throw Error('FFmpeg failed '+code);console.log('Encoded QHD master and web film.');
})().catch(e=>{console.error(e);process.exit(1)});
