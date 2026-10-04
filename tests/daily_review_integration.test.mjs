import test from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {randomBytes} from 'node:crypto';
import {localDate,shiftDate} from '../frontend/domain.js';
const ROOT=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));

test('daily roster shows detailed medals, server ordering, date navigation and ignores stale reports',async()=>{
 const tmp=await mkdtemp(path.join(tmpdir(),'prayer-daily-'));
 const port=23000+Math.floor(Math.random()*1000),base=`http://127.0.0.1:${port}`;
 const server=spawn(process.env.PYTHON||path.join(ROOT,'.venv/bin/python'),['-m','uvicorn','backend.main:app','--host','127.0.0.1','--port',String(port)],{cwd:ROOT,env:{...process.env,PRAYER_DB_PATH:path.join(tmp,'test.sqlite3')},stdio:'ignore'});
 const keys=['fetch','document','window','location','history','FormData','sessionStorage'];
 const originals=Object.fromEntries(keys.map(k=>[k,globalThis[k]]));
 const listeners={},fields={};
 const dummy=()=>({value:'',focus(){},disabled:false,textContent:'',hidden:false,classList:{toggle(){}}});
 const root={innerHTML:'',addEventListener(k,v){listeners[k]=v;},querySelector(k){return fields[k]??=dummy();}};
 let activeToken='',hold=null,reportTransform=null,dayTransform=null;
 const api=async(token,route,method='GET',body)=>{
  const response=await originals.fetch(base+'/api/v1'+route,{method,headers:{'Content-Type':'application/json',Origin:base,...(token?{Authorization:`Bearer ${token}`}:{})},...(body?{body:JSON.stringify(body)}:{})});
  const data=await response.json();assert.ok(response.ok,JSON.stringify(data));return data;
 };
 try{
  for(let i=0;;i++){try{if((await originals.fetch(base+'/api/v1/health')).ok)break;}catch{}assert.ok(i<120,'server started');await delay(25);}
  const register=alias=>api('','/auth/register','POST',{alias,password:randomBytes(24).toString('hex'),...(alias==='daily-teacher'?{account_type:'supervisor'}:{})});
  const teacher=await register('daily-teacher');activeToken=teacher.access_token;
  const children=await Promise.all(['high-record','lower-record','empty-record'].map(register));
  const group=(await api(activeToken,'/groups','POST',{name:'daily-group'})).group;
  const invite=await api(activeToken,`/groups/${group.id}/invite-link`,'POST');
  for(const child of children)await api(child.access_token,'/group-invites/accept','POST',{token:invite.token,request_key:randomBytes(16).toString('hex')});
  const yesterday=shiftDate(localDate(),-1);
  await api(children[0].access_token,`/days/${yesterday}`,'PUT',{version:0,prayers:{fajr:2,dhuhr:2,asr:1,maghrib:0,isha:0},sunnah:{fajr_before:2,dhuhr_before:2,dhuhr_after:2,maghrib_after:0,isha_after:0,witr:true},dhuhr_before_blocks:[false,true]});
  await api(children[1].access_token,`/days/${yesterday}`,'PUT',{version:0,prayers:{fajr:2,dhuhr:0,asr:0,maghrib:0,isha:0},sunnah:{fajr_before:0,dhuhr_before:0,dhuhr_after:0,maghrib_after:0,isha_after:0,witr:false},dhuhr_before_blocks:[false,false]});
  globalThis.document={querySelector(k){return k==='#app'?root:dummy();}};
  globalThis.location={pathname:'/',search:'',hash:'',origin:base};
  globalThis.window={addEventListener(k,v){listeners[k]=v;}};
  globalThis.history={pushState(a,b,url){const u=new URL(url,base);Object.assign(globalThis.location,{search:u.search,hash:u.hash});},replaceState(a,b,url){this.pushState(a,b,url);}};
  globalThis.sessionStorage={getItem(){return null;},setItem(){},removeItem(){}};
  globalThis.fetch=async(url,options={})=>{
   const response=await originals.fetch(base+url,{...options,headers:{...options.headers,Origin:base,...(activeToken?{Authorization:`Bearer ${activeToken}`}:{})}});
   if(hold&&(hold.matches?hold.matches(url,options):String(url).includes('/daily-review'))){const h=hold;hold=null;h.started();await h.promise;}
   if(reportTransform&&String(url).includes('/daily-review')){const data=reportTransform(await response.json());return {ok:response.ok,status:response.status,json:async()=>data};}
   if(dayTransform&&String(url).includes('/members/')&&String(url).includes('/days/')){const data=dayTransform(await response.json());return {ok:response.ok,status:response.status,json:async()=>data};}
   return response;
  };
  await import('../frontend/app.js');
  for(let i=0;!root.innerHTML.includes('data-view="groups"');i++){assert.ok(i<120,'supervisor navigation');await delay(20);}
  const click=dataset=>listeners.click({target:{closest(){return {dataset,disabled:false};}}});
  await click({action:'view',view:'groups'});
  await click({action:'view',view:'report'});
  assert.match(root.innerHTML,new RegExp(`value="${yesterday}"`));
  assert.deepEqual([...root.innerHTML.matchAll(/class="report-heading">([^<]+)</g)].map(match=>match[1]),['العضو','الفجر','الظهر','العصر','المغرب','العشاء','الإجمالي']);
  const studentRow=id=>root.innerHTML.match(new RegExp(`<tr data-member-id="${id}">([\\s\\S]*?)</tr>`))[1];
  const highRow=studentRow(children[0].user.id),emptyRow=studentRow(children[2].user.id);
  for(const row of [highRow,emptyRow]){assert.equal((row.match(/<td(?: |\/?>)/g)||[]).length,6);assert.equal((row.match(/class="report-record /g)||[]).length,12);}
  const labels=[...highRow.matchAll(/class="report-record [^"]*" aria-label="([^"]+)"/g)].map(match=>match[1]);
  assert.deepEqual(labels.map(label=>label.split(':')[0]),['سنة الفجر القبلية','الفجر، الفرض','سنة الظهر القبلية، المجموعة ١','سنة الظهر القبلية، المجموعة ٢','الظهر، الفرض','سنة الظهر البعدية','العصر، الفرض','المغرب، الفرض','سنة المغرب البعدية','العشاء، الفرض','سنة العشاء البعدية','الوتر']);
  assert.match(highRow,/report-record sunnah unrecorded[^>]*aria-label="سنة الظهر القبلية، المجموعة ١: غير مسجّل"[^>]*><span class="report-icon" aria-hidden="true">×<\/span><span class="report-points" aria-hidden="true">&nbsp;<\/span>/);
  assert.match(highRow,/report-record sunnah silver[^>]*aria-label="سنة الظهر القبلية، المجموعة ٢: ٢ ركعة"/);
  assert.match(highRow,/report-record fard gold[^>]*><span class="report-icon" aria-hidden="true">🥇<\/span><span class="report-points" aria-hidden="true">\+٢٧<\/span>/);
  assert.match(highRow,/report-record fard gold[^>]*aria-label="العصر، الفرض: في البيت"[^>]*>[\s\S]*?report-points" aria-hidden="true">\+١<\/span>/);
  assert.match(highRow,/report-record sunnah silver[^>]*aria-label="الوتر: مكتمل"/);
  assert.equal((emptyRow.match(/report-record fard unrecorded/g)||[]).length,5);
  assert.equal((emptyRow.match(/report-record sunnah unrecorded/g)||[]).length,7);
  assert.equal((emptyRow.match(/report-icon" aria-hidden="true">×<\/span>/g)||[]).length,12);
  assert.doesNotMatch(root.innerHTML,/يمكن تمريره أفقياً/);
  assert.ok(root.innerHTML.indexOf('high-record')<root.innerHTML.indexOf('lower-record'));
  assert.match(root.innerHTML,/٣ \/ ٥/);assert.match(root.innerHTML,/🥇 ٥٥/);assert.match(root.innerHTML,/٤ \/ ٧/);
  assert.match(root.innerHTML,/قبل ١/);assert.match(root.innerHTML,/قبل ٢/);assert.match(root.innerHTML,/title="الوتر">وتر/);
  assert.match(root.innerHTML,/لا يوجد سجل محفوظ لهذا اليوم/);
  assert.doesNotMatch(root.innerHTML,/data-action="(?:prayer|sunnah|witr)"/);
  // Eligibility must preserve recorded raw values while hiding future rewards.
  reportTransform=data=>{data.rows[0].day.schedule={eligible_prayers:[]};return data;};
  await click({action:'report-refresh'});
  const futureRow=studentRow(children[0].user.id);
  assert.equal((futureRow.match(/report-record (?:fard|sunnah) not-due/g)||[]).length,12);
  assert.match(futureRow,/سنة الظهر القبلية، المجموعة ٢: لم يحن وقتها/);
  assert.doesNotMatch(futureRow,/report-icon" aria-hidden="true">(?:🥇|🥈|×)/);
  reportTransform=data=>{data.rows[0].day.schedule={eligible_prayers:null};data.rows[0].day.stats={completed:null,total:null,gold_medals:null,silver_medals:null,silver_target:null};return data;};
  await click({action:'report-refresh'});
  const unknownRow=studentRow(children[0].user.id);
  assert.match(unknownRow,/سنة الظهر القبلية، المجموعة ٢: ٢ ركعة، الوقت غير متاح/);
  assert.match(unknownRow,/الفجر، الفرض: في المسجد، الوقت غير متاح/);
  assert.match(unknownRow,/report-record sunnah silver/);
  assert.match(unknownRow,/<strong>— \/ —<\/strong>/);
  reportTransform=null;
  await click({action:'report-refresh'});
  // The first old date response must not overwrite the next date requested.
  let started,release;const start=new Promise(r=>started=r),promise=new Promise(r=>release=r);hold={started,promise};
  const old=click({action:'report-shift',days:'-1'});await start;
  await click({action:'report-shift',days:'-1'});const newest=shiftDate(yesterday,-2);release();await old;
  assert.match(root.innerHTML,new RegExp(`value="${newest}"`));assert.doesNotMatch(root.innerHTML,/🥇 ٥٥/);
  await click({action:'report-yesterday'});assert.match(root.innerHTML,/🥇 ٥٥/);
  await click({action:'report-member',id:String(children[0].user.id)});
  assert.match(root.innerHTML,/العودة إلى سجل المجموعة/);assert.match(root.innerHTML,/high-record/);
  assert.equal((root.innerHTML.match(/class="report-table"/g)||[]).length,1);
  assert.equal((root.innerHTML.match(/class="report-record /g)||[]).length,12);
  assert.match(root.innerHTML,/سنة الظهر القبلية، المجموعة ١: غير مسجّل/);
  assert.match(root.innerHTML,/سنة الظهر القبلية، المجموعة ٢: ٢ ركعة/);
  assert.match(root.innerHTML,/الوتر: مكتمل/);
  assert.match(root.innerHTML,new RegExp(`id="day-date" value="${yesterday}"`));
  assert.doesNotMatch(root.innerHTML,/data-action="(?:prayer|sunnah|witr)"|class="prayer-board"|class="witr-card/);
  await click({action:'view',view:'achievements'});
  assert.match(root.innerHTML,/جدول سجل الأيام/);
  assert.match(root.innerHTML,/سنة الظهر القبلية، المجموعة ١: غير مسجّل/);
  assert.match(root.innerHTML,/سنة الظهر القبلية، المجموعة ٢: ٢ ركعة/);
  assert.match(root.innerHTML,/الوتر: مكتمل/);
  assert.doesNotMatch(root.innerHTML,/history-list|mini-progress/);
  assert.match(root.innerHTML,new RegExp(`data-action="history-date" data-date="${yesterday}"`));
  await click({action:'history-date',date:yesterday});
  assert.match(root.innerHTML,new RegExp(`id="day-date" value="${yesterday}"`));
  assert.match(root.innerHTML,/class="report-table"/);
  assert.match(root.innerHTML,/العودة إلى سجل المجموعة/);
  // Full diary schedules omit eligible_prayers; the shared table uses prayer times.
  dayTransform=day=>({...day,schedule:{today:yesterday,available:true,times:Object.fromEntries(['fajr','dhuhr','asr','maghrib','isha'].map(key=>[key,new Date(Date.now()+3600000).toISOString()]))}});
  await click({action:'history-date',date:yesterday});
  assert.equal((root.innerHTML.match(/report-record (?:fard|sunnah) not-due/g)||[]).length,12);
  assert.match(root.innerHTML,/الوتر: لم يحن وقتها/);
  dayTransform=day=>({...day,schedule:{today:yesterday,available:false}});
  await click({action:'history-date',date:yesterday});
  assert.match(root.innerHTML,/الفجر، الفرض: في المسجد، الوقت غير متاح/);
  assert.match(root.innerHTML,/الوتر: مكتمل، الوقت غير متاح/);
  assert.match(root.innerHTML,/تعذّر جلب مواقيت الصلاة/);
  dayTransform=null;
  await click({action:'view',view:'report'});assert.match(root.innerHTML,new RegExp(`value="${yesterday}"`));
  // A late management response must refresh an active report, not strand its spinner.
  await click({action:'view',view:'account'});
  let unlinkStarted,unlinkRelease;const unlinkStart=new Promise(r=>unlinkStarted=r),unlinkPromise=new Promise(r=>unlinkRelease=r);
  hold={started:unlinkStarted,promise:unlinkPromise,matches:(url,options)=>String(url).endsWith(`/groups/${group.id}/members/${children[2].user.id}`)&&options.method==='DELETE'};
  const unlink=click({action:'remove-member',id:String(children[2].user.id)});await unlinkStart;
  await click({action:'view',view:'report'});unlinkRelease();await unlink;
  await click({action:'view',view:'report'});
  assert.match(root.innerHTML,/high-record/);assert.doesNotMatch(root.innerHTML,/نحمّل سجل الأعضاء/);assert.doesNotMatch(root.innerHTML,/empty-record/);
  await click({action:'report-today'});assert.match(root.innerHTML,/data-action="report-shift" data-days="1"[^>]*disabled/);
  await click({action:'report-shift',days:'1'});assert.match(root.innerHTML,new RegExp(`value="${localDate()}"`));
  // Logout while a report response is in flight cannot expose that report.
  started=null;release=null;const loggedStart=new Promise(r=>started=r),loggedPromise=new Promise(r=>release=r);hold={started,promise:loggedPromise};
  const late=click({action:'report-yesterday'});await loggedStart;await click({action:'logout'});activeToken='';release();await late;
  assert.match(root.innerHTML,/auth-form/);assert.doesNotMatch(root.innerHTML,/high-record/);
 }finally{
  for(const k of keys){if(originals[k]===undefined)delete globalThis[k];else globalThis[k]=originals[k];}
  server.kill('SIGTERM');await new Promise(resolve=>{if(server.exitCode!==null)resolve();else server.once('exit',resolve);});await rm(tmp,{recursive:true,force:true});
 }
});
