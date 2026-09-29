/* Actual application DOM textures + measured interaction targets, offline QHD.
 * Usage: node render_product_film.cjs OUTPUT FONT_DIR FFMPEG CAPTURES [--stills]
 */
const {createCanvas,GlobalFonts,loadImage}=require('@napi-rs/canvas');
const fs=require('node:fs'),path=require('node:path'),{spawn}=require('node:child_process'),{once}=require('node:events');
const [out,fontDir,ffmpeg,captureDir,...flags]=process.argv.slice(2);
if(!captureDir)throw Error('Expected OUTPUT FONT_DIR FFMPEG CAPTURES');
fs.mkdirSync(out,{recursive:true});
for(const [file,alias] of [['Regular','Paper'],['SemiBold','Semi'],['ExtraBold','Bold']])if(!GlobalFonts.registerFromPath(path.join(fontDir,`Paperlogy-${file}.ttf`),alias))throw Error('Missing font '+file);
const W=2560,H=1440,FPS=30,DURATION=60;
const manifest=JSON.parse(fs.readFileSync(path.join(captureDir,'captures.json'),'utf8'));
const chapters=[
 ['탐색','필요한 공고를 찾고','검색어를 입력하고, 검토할 공고를 여세요.','search-empty','search-result','공고명·기관·공고번호로 검색','필요한 공고를 선택해 상세 확인'],
 ['문서 분석','AI가 읽은 조건, 원문으로 확인','요약에서 근거까지, 하나의 검토 흐름.','detail','evidence','공고 상세의 검토 요약 확인','주요 확인 사항에서 문구와 출처 확인'],
 ['회사 비교','조건과 점수를 구분해서','참가자격 · 정량 점수 · 주의사항을 함께 검토합니다.','evidence','quant','조건별 근거를 확인','정량 점수와 추가 확인 사항을 별도 검토'],
 ['담당자 판단','마지막 판단은, 담당자가','참여·보류·불참과 판단 이유를 기록합니다.','decision-empty','decision-note','담당자 결정 영역 열기','판단 사유를 실제 입력란에 입력'],
 ['참고자료','과거의 기록까지 연결','유사사업의 낙찰 이력을 이번 검토에 참고하세요.','detail','history','같은 공고의 검토 맥락 유지','최근 3년 낙찰 이력과 출처 확인'],
 ['결과 기록','입찰 이후의 결과까지','확인된 결과와 출처를 함께 남깁니다.','result-empty','result-filled','실제 결과 입력창에서 결과 선택','공식 결과를 확인한 출처 입력'],
 ['Teams 알림','업무가 있는 곳으로, Teams','관심 공고 알림과 부서 맞춤 브리핑.','teams','teams','PAI의 실제 Teams 카드 미리보기','자격·마감·입찰 판단에서 상세 근거로'],
 ['업무 관리','다음 할 일이 한눈에','판단부터 마감 관리, 입찰 결과까지.','dashboard','dashboard','오늘 할 일에서 검토 우선순위 확인','판단 · 진행 · 결과의 흐름을 놓치지 않게'],
];
const ink='#123e5b',muted='#5e7789',blue='#165e93';let c;const images={};
const clamp=(n,a=0,b=1)=>Math.max(a,Math.min(b,n)),ease=n=>{n=clamp(n);return n*n*(3-2*n)},mix=(a,b,t)=>a+(b-a)*t;
function box(x,y,w,h,r,fill,stroke){c.beginPath();c.roundRect(x,y,w,h,r);if(fill){c.fillStyle=fill;c.fill()}if(stroke){c.strokeStyle=stroke;c.lineWidth=.7;c.stroke()}}
function text(s,x,y,size=16,color=ink,font='Paper',align='left',baseline='top'){c.font=`${size}px ${font}`;c.fillStyle=color;c.textAlign=align;c.textBaseline=baseline;c.fillText(s,x,y)}
function glass(x,y,w,h,r=16){c.save();c.shadowColor='#46667f1d';c.shadowBlur=18;c.shadowOffsetY=8;const g=c.createLinearGradient(x,y,x+w,y+h);g.addColorStop(0,'#ffffffce');g.addColorStop(.5,'#eff7fb63');g.addColorStop(1,'#ffffffac');box(x,y,w,h,r,g,'#ffffffd9');c.restore();box(x+2,y+2,w-4,h-4,r-2,null,'#c7dfe95c')}
function backdrop(t){const g=c.createLinearGradient(0,0,1280,720);g.addColorStop(0,'#f4f7f9');g.addColorStop(.6,'#e9f0f5');g.addColorStop(1,'#dfeaf2');c.fillStyle=g;c.fillRect(0,0,1280,720);const light=c.createRadialGradient(830+Math.sin(t*.2)*80,170,10,830,170,620);light.addColorStop(0,'#ffffffde');light.addColorStop(1,'#ffffff00');c.fillStyle=light;c.fillRect(0,0,1280,720)}
function cursor(x,y,click,opacity){c.save();c.globalAlpha=opacity;if(click>0&&click<1){c.beginPath();c.arc(x,y,9+click*20,0,Math.PI*2);c.strokeStyle=`rgba(51,136,190,${(1-click)*.55})`;c.lineWidth=2;c.stroke()}c.translate(x,y);c.shadowColor='#082b5870';c.shadowBlur=5;c.shadowOffsetY=3;c.beginPath();c.moveTo(0,0);c.lineTo(1,24);c.lineTo(7,18);c.lineTo(13,29);c.lineTo(18,26);c.lineTo(12,16);c.lineTo(22,14);c.closePath();c.fillStyle='#153c58';c.fill();c.strokeStyle='#fff';c.lineWidth=2;c.lineJoin='round';c.stroke();c.restore()}
const screen=createCanvas(2120,1325);
function cameraRect(name,zoom){const f=manifest.frames[name],target=f.focus;let w=1440/zoom,h=w/1.6,cx=720,cy=450;if(target){cx=target.x+target.width/2;cy=target.y+target.height/2}if(name==='teams'&&f.crop){cx=f.crop.x+f.crop.width/2;cy=f.crop.y+f.crop.height/2}return{x:clamp(cx-w/2,0,1440-w),y:clamp(cy-h/2,0,900-h),w,h}}
function screenFrame(name,time,zoom,alpha){const old=c;c=screen.getContext('2d');c.save();c.scale(2,2);const crop=cameraRect(name,zoom),sx=1060/crop.w,sy=662.5/crop.h;c.drawImage(images[name],crop.x*2,crop.y*2,crop.w*2,crop.h*2,0,0,1060,662.5);const r=manifest.frames[name].focus;if(r&&r.width<1400&&r.height<600){const x=(r.x-crop.x)*sx,y=(r.y-crop.y)*sy,w=r.width*sx,h=r.height*sy;c.save();c.globalAlpha=alpha;c.shadowColor='#3e9fcb36';c.shadowBlur=12;const g=c.createLinearGradient(x,y,x+w,y+h);g.addColorStop(0,'#def4ff36');g.addColorStop(.5,'#ffffff08');g.addColorStop(1,'#a9d4ee21');box(x-3,y-3,w+6,h+6,9,g,'#fff');c.restore();const move=ease(time/.85),tx=clamp(x+Math.min(w*.76,w-12),30,1016),ty=clamp(y+h*.6,20,622);cursor(mix(1018,tx,move),mix(590,ty,move),time>.9&&time<1.55?(time-.9)/.65:0,clamp(time/.25))}c.restore();c=old;return screen}
function device(texture,t,front){
 // Uninterrupted display inside an opened fold-style exterior. No hinge/crease.
 const tilt=(1-front)*.035; c.save();c.translate(640,357);c.transform(1,tilt,-tilt*.23,1,0,0);
 const x=-416,y=-260,w=832,h=520;
 c.save();c.shadowColor='#17364c38';c.shadowBlur=28;c.shadowOffsetY=20;box(x-12,y-12,w+24,h+29,37,'#8195a4');c.restore();
 const metal=c.createLinearGradient(x,y,x+w,y+h);metal.addColorStop(0,'#f2f9fe');metal.addColorStop(.04,'#94a7b7');metal.addColorStop(.5,'#d6e2eb');metal.addColorStop(.95,'#f7fcff');metal.addColorStop(1,'#677e90');box(x-10,y-10,w+20,h+20,35,metal,'#b9cbd8');box(x-6,y-6,w+12,h+12,31,'#10202b');
 c.save();c.beginPath();c.roundRect(x,y,w,h,25);c.clip();c.drawImage(texture,x,y,w,h);const shine=c.createLinearGradient(x,y,x+w,y+h);shine.addColorStop(0,'#ffffff14');shine.addColorStop(.15,'#ffffff00');shine.addColorStop(.86,'#ffffff00');shine.addColorStop(1,'#ffffff0b');box(x,y,w,h,25,shine);c.restore();
 box(x+w+10,y+65,3,46,2,'#839aab');box(x+w+10,y+126,3,61,2,'#9cabb8');c.restore();
}
function chapterFrame(target,i,local){c=target.getContext('2d');c.save();c.scale(2,2);backdrop(i+local);const ch=chapters[i],phase=local<3.1?0:1;glass(46,27,83,34,12);text(String(i+1).padStart(2,'0')+' / 08',87.5,44,14,blue,'Semi','center','middle');text(ch[0],145,36,15,muted,'Semi');text(ch[1],640,31,27,ink,'Bold','center');text('PAI.',1232,33,23,blue,'Bold','right');let name=ch[3+phase];if(i===0&&local>.8&&local<3.1)name='search-type-'+Math.min(6,Math.max(1,Math.floor((local-.8)*3.2)+1));if(i===3&&local>3.1&&local<5.9)name='decision-type-'+Math.min(8,Math.max(1,Math.floor((local-3.1)*3)+1));const p=phase?local-3.1:local,close=ease((local-.8)/1.3)*(1-ease((local-5.5)/.9));device(screenFrame(name,p,1+close*.22,ease(p/.55)*.78),local,close);text(ch[5+phase],640,640,24,ink,'Semi','center');text(ch[2],640,676,14,muted,'Paper','center');text('실제 PAI 화면 · 예시 데이터',48,692,10,'#7890a1');box(1084,699,144,2,1,'#bfced9');box(1084,699,144*(i+local/6.75)/8,2,1,blue);c.restore()}
function bookend(target,end,t){c=target.getContext('2d');c.save();c.scale(2,2);backdrop(t);const arrival=ease(t/1.2);c.save();c.globalAlpha=arrival;text('PAI',624,242+(1-arrival)*16,145,blue,'Bold','center');text('.',749,242+(1-arrival)*16,145,'#4d9ebb','Bold','center');c.restore();c.save();c.globalAlpha=ease((t-.45)/.8);text('AI PUBLIC BIDDING',640,476,12,muted,'Semi','center');text(end?'검토는 깊게. 판단은 명확하게.':'공고에서 판단까지.',640,505,35,ink,'Bold','center');text(end?'pai.kma.or.kr':'공공입찰 검토의 흐름을 하나로',640,561,20,muted,'Paper','center');c.restore();c.restore()}
const final=createCanvas(W,H),previous=createCanvas(W,H);
function render(t){if(t<3){bookend(final,false,t);return final}if(t>=57){bookend(final,true,t-57);return final}const i=Math.min(7,Math.floor((t-3)/6.75)),local=t-3-i*6.75;chapterFrame(final,i,local);if(local<.35){if(i===0)bookend(previous,false,3);else chapterFrame(previous,i-1,6.74);const f=final.getContext('2d');f.save();f.globalAlpha=1-ease(local/.35);f.drawImage(previous,0,0);f.restore()}return final}
(async()=>{
 await Promise.all(Object.entries(manifest.frames).map(async([name,f])=>images[name]=await loadImage(path.join(captureDir,f.file))));
 for(let i=0;i<8;i++)for(const [label,offset]of[['wide',1.5],['close',4.8]])fs.writeFileSync(path.join(out,`scene-${i+1}-${label}.png`),render(3+i*6.75+offset).encodeSync('png'));
 fs.writeFileSync(path.join(out,'intro.png'),render(2.3).encodeSync('png'));fs.writeFileSync(path.join(out,'outro.png'),render(59.5).encodeSync('png'));fs.writeFileSync(path.join(out,'pai-product-poster.webp'),render(7.8).encodeSync('webp',94));
 fs.writeFileSync(path.join(out,'tour-manifest.json'),JSON.stringify({width:W,height:H,fps:FPS,duration:DURATION,synthetic:true,source:manifest.source,chapters:chapters.map((s,i)=>({start:3+i*6.75,title:s[0],captures:s.slice(3,5)}))},null,2));
 if(flags.includes('--stills'))return;
 const log=fs.openSync(path.join(out,'encode.log'),'w');const args=['-y','-f','rawvideo','-pix_fmt','rgba','-s',`${W}x${H}`,'-r',String(FPS),'-i','pipe:0','-an','-c:v','libx264','-preset','fast','-crf','17','-pix_fmt','yuv420p','-movflags','+faststart',path.join(out,'pai-product-tour-qhd.mp4'),'-an','-c:v','libvpx-vp9','-b:v','0','-crf','25','-cpu-used','5','-row-mt','1','-threads','6','-pix_fmt','yuv420p',path.join(out,'pai-product-tour.webm')];
 const child=spawn(ffmpeg,args,{stdio:['pipe','ignore',log],windowsHide:true}),completed=once(child,'close');child.stdin.on('error',e=>console.error(e.message));
 for(let n=0;n<FPS*DURATION;n++){if(!child.stdin.write(render(n/FPS).data()))await once(child.stdin,'drain');if(n%(FPS*5)===0)console.log(`render ${n/FPS}/${DURATION}s`)}child.stdin.end();const[code]=await completed;fs.closeSync(log);if(code!==0)throw Error('FFmpeg failed '+code);console.log('Encoded QHD master and web film.');
})().catch(e=>{console.error(e);process.exit(1)});

