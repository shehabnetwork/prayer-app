// Runs the real frontend event handlers against the real REST API using a tiny DOM
// adapter. This verifies application logic and generated semantic markup, not layout.
import test from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {randomBytes} from 'node:crypto';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {shiftDate} from '../frontend/domain.js';

const ROOT=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));

test('frontend registration, prayer/sunnah controls, history and auth use the live API',async()=>{
 const tmp=await mkdtemp(path.join(tmpdir(),'prayer-ui-logic-'));
 const port=19000+Math.floor(Math.random()*1000);
 const base=`http://127.0.0.1:${port}`;
 const server=spawn(process.env.PYTHON||'python',['-m','uvicorn','backend.main:app','--host','127.0.0.1','--port',String(port)],{cwd:ROOT,env:{...process.env,PRAYER_DB_PATH:path.join(tmp,'test.sqlite3')},stdio:'ignore'});
 const originals={fetch:globalThis.fetch,document:globalThis.document,window:globalThis.window,location:globalThis.location,history:globalThis.history,FormData:globalThis.FormData};
 let cookie='';
 let holdResponse=null;
 const listeners={};
 const fields={};
 const dummyField=()=>({value:'',disabled:false,textContent:'',focus(){},classList:{toggle(){}},hidden:true});
 const root={innerHTML:'',addEventListener(name,handler){listeners[name]=handler;},querySelector(selector){return fields[selector]??=(dummyField());}};
 const toast=dummyField();
 try{
  for(let i=0;i<100;i++){try{if((await originals.fetch(base+'/api/v1/health')).ok)break;}catch{}await delay(30);if(i===99)throw Error('test server did not start');}
  globalThis.document={querySelector(selector){return selector==='#app'?root:toast;}};
  globalThis.location={pathname:'/',search:'',hash:''};
  globalThis.window={addEventListener(name,handler){listeners[name]=handler;}};
  globalThis.history={pushState(_a,_b,url){const u=new URL(url,base);Object.assign(globalThis.location,{search:u.search,hash:u.hash});},replaceState(_a,_b,url){this.pushState(_a,_b,url);}};
  globalThis.FormData=class{constructor(form){this.form=form;}get(key){return this.form.values[key];}};
  globalThis.fetch=async(url,options={})=>{
   const headers={...options.headers,Origin:base,...(cookie?{Cookie:cookie}:{})};
   const response=await originals.fetch(base+url,{...options,headers});
   if(holdResponse&&holdResponse.matches(url,options)){const hold=holdResponse;holdResponse=null;hold.started();await hold.promise;}
   const setCookie=response.headers.get('set-cookie');if(setCookie)cookie=setCookie.split(';')[0];
   return response;
  };
  await import('../frontend/app.js');
  for(let i=0;!root.innerHTML.includes('auth-form');i++){assert.ok(i<100,'initial auth render');await delay(20);}
  const click=async dataset=>listeners.click({target:{closest(){return {dataset,disabled:false};}}});
  await click({action:'auth-mode',mode:'register'});
  const form={id:'auth-form',values:{alias:'بطل_اختبار',password:randomBytes(24).toString('base64url')},querySelector(){return dummyField();}};
  await listeners.submit({target:form,preventDefault(){}});
  assert.equal((root.innerHTML.match(/class="prayer-row /g)||[]).length,5);
  assert.equal((root.innerHTML.match(/class="character-button /g)||[]).length,5);
  assert.equal((root.innerHTML.match(/class="witr-card /g)||[]).length,1);
  assert.match(root.innerHTML,/١٣٥/,'daily summary should show the 135 gold target');
  assert.match(root.innerHTML,/٧/,'daily summary should show the 7 silver target');
  const snapshot=async()=> (await globalThis.fetch('/api/v1/days/'+new URLSearchParams(globalThis.location.search).get('date'))).json();
  for(const expected of [1,2,0]){await click({action:'prayer',key:'fajr'});assert.equal((await snapshot()).prayers.fajr,expected);}
  await click({action:'witr'});assert.equal((await snapshot()).stats.percent,0);
  await click({action:'sunnah',key:'dhuhr_before',block:'1'});
  assert.deepEqual((await snapshot()).dhuhr_before_blocks,[false,true]);
  await click({action:'sunnah',key:'dhuhr_before',block:'0'});
  assert.equal((await snapshot()).sunnah.dhuhr_before,4);
  await click({action:'sunnah',key:'dhuhr_before',block:'1'});
  assert.equal((await snapshot()).sunnah.dhuhr_before,2);
  for(const key of ['fajr','dhuhr','maghrib'])await click({action:'prayer',key});
  await click({action:'prayer',key:'maghrib'});
  assert.deepEqual((await snapshot()).stats,{completed:3,total:5,home:2,mosque:1,sunnah_rakahs:2,witr:true,gold_medals:29,silver_medals:2,percent:60});
  assert.ok(root.innerHTML.includes('٦٠٪'));
  const today=new URLSearchParams(globalThis.location.search).get('date');
  await click({action:'date-shift',days:'-1'});assert.equal((await snapshot()).stats.percent,0);
  await click({action:'history-date',date:today});assert.equal((await snapshot()).stats.percent,60);

  // Hold an old-day mutation response while navigating to a different date.
  let release,started;const startedPromise=new Promise(resolve=>started=resolve);
  holdResponse={matches:(url,options)=>url.includes('/prayers/asr/cycle'),promise:new Promise(resolve=>release=resolve),started};
  const inFlight=click({action:'prayer',key:'asr'});await startedPromise;
  await click({action:'date-shift',days:'-1'});
  assert.ok(root.innerHTML.includes('٠٪'));
  release();await inFlight;
  assert.ok(root.innerHTML.includes('٠٪'),'old-day mutation must not replace currently selected day');
  assert.equal((await snapshot()).prayers.asr,0);
  await click({action:'history-date',date:today});
  assert.equal((await snapshot()).prayers.asr,1,'old-day mutation still persisted on its own day');
  // Return the sample to three completed prayers for the remaining flow.
  await click({action:'prayer',key:'asr'});await click({action:'prayer',key:'asr'});
  // Hold a read response for today while navigating to yesterday.
  const prior=shiftDate(today,-1);
  let releaseRead,readStarted;const readStartedPromise=new Promise(resolve=>readStarted=resolve);
  await click({action:'history-date',date:prior});
  holdResponse={matches:(url,options)=>url===`/api/v1/days/${today}`,promise:new Promise(resolve=>releaseRead=resolve),started:readStarted};
  const staleRead=click({action:'history-date',date:today});await readStartedPromise;
  await click({action:'history-date',date:prior});releaseRead();await staleRead;
  assert.ok(root.innerHTML.includes('٠٪'),'stale read must not replace currently selected day');
  await click({action:'history-date',date:today});


  // A read started by account A must not leak into newly signed-in account B.
  await click({action:'history-date',date:prior});
  let releaseIdentity,identityStarted;const identityStartedPromise=new Promise(resolve=>identityStarted=resolve);
  holdResponse={matches:(url,options)=>url===`/api/v1/days/${today}`,promise:new Promise(resolve=>releaseIdentity=resolve),started:identityStarted};
  const oldAccountRead=click({action:'history-date',date:today});await identityStartedPromise;
  await click({action:'logout'});await click({action:'auth-mode',mode:'register'});
  const otherForm={id:'auth-form',values:{alias:'قمر_اختبار',password:randomBytes(24).toString('base64url')},querySelector(){return dummyField();}};
  await listeners.submit({target:otherForm,preventDefault(){}});
  assert.ok(root.innerHTML.includes('٠٪'));
  releaseIdentity();await oldAccountRead;
  assert.ok(root.innerHTML.includes('٠٪'),'old-account read must not leak into a new account');
  assert.ok(root.innerHTML.includes('قمر_اختبار'));
  await click({action:'logout'});await listeners.submit({target:form,preventDefault(){}});
  assert.ok(root.innerHTML.includes('٦٠٪'));

  await click({action:'view',view:'achievements'});assert.equal((root.innerHTML.match(/class="achievement-badge /g)||[]).length,4);assert.equal((root.innerHTML.match(/class="history-row"/g)||[]).length,1);
  assert.equal((root.innerHTML.match(/data-action="achievement-period"/g)||[]).length,3);
  await click({action:'achievement-period',period:'week'});assert.match(root.innerHTML,/هذا الأسبوع/);
  await click({action:'achievement-period',period:'month'});assert.match(root.innerHTML,/هذا الشهر/);
  await click({action:'achievement-period',period:'last30'});assert.match(root.innerHTML,/آخر ٣٠ يومًا/);
  // A second recorded day adds another full daily medal target to the cards.
  const previousDay=await globalThis.fetch(`/api/v1/days/${prior}/prayers/fajr`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({state:1})});
  assert.equal(previousDay.status,200);
  await click({action:'view',view:'achievements'});
  assert.ok(root.innerHTML.includes('الميداليات الذهبية: ٣٠ من ٢٧٠'),'gold card includes both recorded daily targets');
  assert.ok(root.innerHTML.includes('الميداليات الفضية: ٢ من ١٤'),'silver card includes both recorded daily targets');
  assert.ok(root.innerHTML.includes('الميداليات الذهبية: ٢٩ من ١٣٥'),'history gold uses the individual day target');
  assert.ok(root.innerHTML.includes('الميداليات الفضية: ٢ من ٧'),'history silver uses the individual day target');
  assert.ok(root.innerHTML.includes('الميداليات الذهبية: ١ من ١٣٥'),'second day retains its own gold target');
  assert.ok(root.innerHTML.includes('الميداليات الفضية: ٠ من ٧'),'zero silver still shows the daily target');
  await click({action:'logout'});assert.ok(root.innerHTML.includes('auth-form'));
  await listeners.submit({target:form,preventDefault(){}});assert.ok(root.innerHTML.includes('٦٠٪'));
 }finally{
  Object.assign(globalThis,originals);
  server.kill('SIGTERM');
  await new Promise(resolve=>{if(server.exitCode!==null)resolve();else server.once('exit',resolve);});
  await rm(tmp,{recursive:true,force:true});
 }
});
