// Exercise supervisor flows through the real frontend handlers and real API.
import test from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {randomBytes} from 'node:crypto';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const ROOT=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const password=()=>randomBytes(24).toString('base64url');

test('supervisor links, readonly child views, relationship changes, and stale reads',async()=>{
 const tmp=await mkdtemp(path.join(tmpdir(),'prayer-supervisors-'));
 const port=20000+Math.floor(Math.random()*1000);
 const base=`http://127.0.0.1:${port}`;
 const server=spawn(process.env.PYTHON||path.join(ROOT,'.venv/bin/python'),['-m','uvicorn','backend.main:app','--host','127.0.0.1','--port',String(port)],{cwd:ROOT,env:{...process.env,PRAYER_DB_PATH:path.join(tmp,'test.sqlite3')},stdio:'ignore'});
 const originals={fetch:globalThis.fetch,document:globalThis.document,window:globalThis.window,location:globalThis.location,history:globalThis.history,FormData:globalThis.FormData,sessionStorage:globalThis.sessionStorage};
 let cookie='',registeredToken='',inviteResponse=null,holdResponse=null,authRequestCount=0;
 const listeners={},fields={},storage=new Map();
 let manualCopySelected=false;
 const dummyField=()=>({value:'',disabled:false,textContent:'',focus(){},select(){manualCopySelected=true;},classList:{toggle(){}}});
 const root={innerHTML:'',addEventListener(name,handler){listeners[name]=handler;},querySelector(selector){return fields[selector]??=(dummyField());}};
 const toast=dummyField();
 const request=async(token,url,options={})=>{
  const headers={...options.headers,Origin:base,...(token?{Authorization:`Bearer ${token}`}:{})};
  const response=await originals.fetch(base+url,{...options,headers});
  const body=await response.json();
  return {status:response.status,body};
 };
 try{
  for(let i=0;i<120;i++){try{if((await originals.fetch(base+'/api/v1/health')).ok)break;}catch{}await delay(25);if(i===119)throw Error('test server did not start');}
  const createAccount=async(alias,type='child')=>{
   const secret=password();
   const result=await request(null,'/api/v1/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({alias,password:secret,...(type==='supervisor'?{account_type:'supervisor'}:{})})});
   assert.equal(result.status,201,JSON.stringify(result.body));return {...result.body,password:secret};
  };
  const parent=await createAccount('link-parent','supervisor');
  const parentInvite=(await request(parent.access_token,'/api/v1/supervisor/invites',{method:'POST'})).body;
  const secondSupervisor=await createAccount('other-parent','supervisor');
  const secondInvite=(await request(secondSupervisor.access_token,'/api/v1/supervisor/invites',{method:'POST'})).body;

  globalThis.document={querySelector(selector){return selector==='#app'?root:toast;}};
  globalThis.location={pathname:'/',origin:base,search:'',hash:`#invite=${parentInvite.token}`};
  globalThis.window={addEventListener(name,handler){listeners[name]=handler;}};
  globalThis.history={pushState(_a,_b,url){const u=new URL(url,base);Object.assign(globalThis.location,{search:u.search,hash:u.hash});},replaceState(_a,_b,url){this.pushState(_a,_b,url);}};
  globalThis.sessionStorage={getItem(key){return storage.get(key)??null;},setItem(key,value){storage.set(key,String(value));},removeItem(key){storage.delete(key);}};
  globalThis.FormData=class{constructor(form){this.form=form;}get(key){return this.form.values[key];}};
  globalThis.fetch=async(url,options={})=>{
   if(url==='/api/v1/auth/login')authRequestCount++;
   const headers={...options.headers,Origin:base,...(cookie?{Cookie:cookie}:{})};
   const response=await originals.fetch(base+url,{...options,headers});
   if(url==='/api/v1/auth/register'&&response.ok)registeredToken=(await response.clone().json()).access_token;
   if(url.includes('/supervisor/invites')&&options.method==='POST'&&!url.endsWith('/rotate')&&response.ok)inviteResponse=await response.clone().json();
   if(holdResponse&&holdResponse.matches(url,options)){const hold=holdResponse;holdResponse=null;hold.started();await hold.promise;}
   const setCookie=response.headers.get('set-cookie');if(setCookie)cookie=setCookie.split(';')[0];
   return response;
  };
  await import('../frontend/app.js');
  for(let i=0;!root.innerHTML.includes('auth-form');i++){assert.ok(i<120,'initial auth render');await delay(20);}
  const click=async dataset=>listeners.click({target:{closest(){return {dataset,disabled:false};}}});
  await click({action:'auth-mode',mode:'register'});
  const childForm={id:'auth-form',values:{alias:'طفل_الأول',password:password(),account_type:'child'},querySelector(){return dummyField();}};
  await listeners.submit({target:childForm,preventDefault(){}});
  for(let i=0;!root.innerHTML.includes(parent.user.alias);i++){assert.ok(i<100,'invite preview after registration');await delay(20);}
  assert.match(root.innerHTML,/للقراءة فقط/);
  assert.doesNotMatch(root.innerHTML,/تمت إضافة المشرف/);
  await click({action:'accept-invite'});
  for(let i=0;!root.innerHTML.includes('المشرفون على دفتري');i++){assert.ok(i<100,'accepted invite returns to account');await delay(20);}
  let relationships=await request(registeredToken,'/api/v1/account/supervisors');
  assert.deepEqual(relationships.body.supervisors.map(item=>item.id),[parent.user.id]);

  // A child can accept a second supervisor, then remove just the first from the UI.
  const secondAcceptance=await request(registeredToken,'/api/v1/supervisor-invites/accept',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:secondInvite.token,request_key:randomBytes(20).toString('base64url')})});
  assert.equal(secondAcceptance.status,200,JSON.stringify(secondAcceptance.body));
  await click({action:'refresh-management'});
  assert.match(root.innerHTML,/other-parent/);
  await click({action:'remove-supervisor',id:String(parent.user.id)});
  relationships=await request(registeredToken,'/api/v1/account/supervisors');
  assert.deepEqual(relationships.body.supervisors.map(item=>item.id),[secondSupervisor.user.id]);

  await click({action:'enable-supervision'});
  await click({action:'create-invite'});
  assert.ok(inviteResponse?.token,`UI should create a reusable supervisor link: ${root.innerHTML.slice(-800)}`);
  await click({action:'copy-invite'});
  assert.ok(manualCopySelected,'clipboard fallback should select the readonly link');
  assert.match(toast.textContent,/انسخه يدويًا/);
  const children=[];
  for(const alias of ['دفتر_ألف','دفتر_باء']){
   const child=await createAccount(alias);
   const accepted=await request(child.access_token,'/api/v1/supervisor-invites/accept',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:inviteResponse.token,request_key:randomBytes(20).toString('base64url')})});
   assert.equal(accepted.status,200,JSON.stringify(accepted.body));children.push({...child.user,access_token:child.access_token});
  }
  const today=new Date().toISOString().slice(0,10);
  // Seed one day for the first supervised child using its issued account token.
  const seed=await request(children[0].access_token,`/api/v1/days/${today}/prayers/fajr`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({state:2})});
  assert.equal(seed.status,200,JSON.stringify(seed.body));
  await click({action:'refresh-management'});
  assert.ok(root.innerHTML.includes('دفتر_ألف')&&root.innerHTML.includes('دفتر_باء'));

  // Daily and achievement pages for a supervised child show compact read-only records.
  await click({action:'select-child',id:String(children[0].id)});
  assert.match(root.innerHTML,/دفتر دفتر_ألف/);
  assert.match(root.innerHTML,/للقراءة فقط/);
  assert.match(root.innerHTML,/class="report-table"/);
  assert.doesNotMatch(root.innerHTML,/data-action="(?:prayer|sunnah|witr)"/);
  assert.match(root.innerHTML,/دفتر الطفل للقراءة فقط/);
  await click({action:'view',view:'achievements'});
  assert.match(root.innerHTML,/إنجازاتي/);
  assert.match(root.innerHTML,/الميداليات الذهبية/);
  assert.match(root.innerHTML,/دفتر دفتر_ألف/);

  // A late day read for child A must not replace the selected child's day for child B.
  let releaseA,startedA;const startedAPromise=new Promise(resolve=>startedA=resolve);
  holdResponse={matches:url=>url.includes(`/supervisor/children/${children[0].id}/days/`),promise:new Promise(resolve=>releaseA=resolve),started:startedA};
  const staleChildRead=click({action:'select-child',id:String(children[0].id)});
  await startedAPromise;
  await click({action:'select-child',id:String(children[1].id)});
  assert.match(root.innerHTML,/دفتر دفتر_باء/);
  releaseA();await staleChildRead;
  assert.match(root.innerHTML,/دفتر دفتر_باء/,'late read must not change selected child context');

  // Likewise, a read from the previous session must not leak after logout and login.
  let releaseLogout,startedLogout;const startedLogoutPromise=new Promise(resolve=>startedLogout=resolve);
  holdResponse={matches:url=>url.includes(`/supervisor/children/${children[1].id}/days/`),promise:new Promise(resolve=>releaseLogout=resolve),started:startedLogout};
  const staleLogoutRead=click({action:'select-child',id:String(children[1].id)});
  await startedLogoutPromise;
  await click({action:'view',view:'account'});
  await click({action:'logout'});
  assert.ok(root.innerHTML.includes('auth-form'));
  const loginForm={id:'auth-form',values:{alias:'replacement-user',password:''},querySelector(){return dummyField();}};
  // Register a fresh account to prove the previous request cannot leak into a new session.
  const replacementPassword=password();
  const replacement=await request(null,'/api/v1/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({alias:'replacement-user',password:replacementPassword})});
  assert.equal(replacement.status,201,JSON.stringify(replacement.body));
  loginForm.values={alias:'replacement-user',password:replacementPassword};
  await listeners.submit({target:loginForm,preventDefault(){}});
  for(let i=0;!root.innerHTML.includes('replacement-user');i++){assert.ok(i<100,'replacement login render');await delay(20);}
  releaseLogout();await staleLogoutRead;
  assert.ok(root.innerHTML.includes('replacement-user'));
  assert.doesNotMatch(root.innerHTML,/دفتر دفتر_باء/);

  // Canceling a pending invite must not allow a second auth request to race its response.
  await click({action:'logout'});
  globalThis.location.hash=`#invite=${parentInvite.token}`;
  await listeners.hashchange();
  let releaseAuth,startedAuth;const startedAuthPromise=new Promise(resolve=>startedAuth=resolve);
  holdResponse={matches:url=>url==='/api/v1/auth/login',promise:new Promise(resolve=>releaseAuth=resolve),started:startedAuth};
  const requestsBeforeRace=authRequestCount;
  const firstLogin={id:'auth-form',values:{alias:'link-parent',password:parent.password},querySelector(){return dummyField();}};
  const firstAuth= listeners.submit({target:firstLogin,preventDefault(){}});
  await startedAuthPromise;
  globalThis.location.hash=`#invite=${secondInvite.token}`;
  await listeners.hashchange();
  const secondLogin={id:'auth-form',values:{alias:'other-parent',password:secondSupervisor.password},querySelector(){return dummyField();}};
  await listeners.submit({target:secondLogin,preventDefault(){}});
  try{assert.equal(authRequestCount,requestsBeforeRace+1,'a second login must remain blocked while the first response is pending');}
  finally{releaseAuth();await firstAuth;}
 }finally{
  for(const [key,value] of Object.entries(originals))Object.defineProperty(globalThis,key,{configurable:true,writable:true,value});
  if(server.exitCode===null&&server.signalCode===null){
   const exited=new Promise(resolve=>server.once('exit',resolve));
   server.kill('SIGTERM');
   await Promise.race([exited,delay(3000)]);
  }
  await rm(tmp,{recursive:true,force:true});
 }
});

test('late bootstrap /auth/me cannot replace a newer login',async()=>{
 const run=async(releaseMeDuringLogin,runId)=>{
  const originalDescriptors=Object.fromEntries(['fetch','document','window','location','history','FormData','sessionStorage'].map(key=>[key,Object.getOwnPropertyDescriptor(globalThis,key)]));
  const listeners={},fields={},storage=new Map(),root={innerHTML:'',addEventListener(name,handler){listeners[name]=handler;},querySelector(selector){return fields[selector]??=( {value:'',disabled:false,textContent:'',focus(){},classList:{toggle(){}}} );}};
  const toast={textContent:'',classList:{toggle(){}},hidden:true};
  let activeSession=null,daySession=null;
  let signalMeStarted;
  const meStartedPromise=new Promise(resolve=>signalMeStarted=resolve);
  const meStarted=()=>signalMeStarted();
  let meJsonRead;
  const meParsedPromise=new Promise(resolve=>meJsonRead=resolve);
  let resolveMeFetch,resolveLogin,resolveLoginStarted;
  const meFetchPromise=new Promise(resolve=>resolveMeFetch=resolve);
  const loginStartedPromise=new Promise(resolve=>resolveLoginStarted=resolve);
  const loginFetchPromise=new Promise(resolve=>resolveLogin=resolve);
  const loginDeferred=releaseMeDuringLogin;
  const loginWait=loginDeferred?loginFetchPromise:Promise.resolve();
  let calls=0;
  const jsonResponse=value=>new Response(JSON.stringify(value),{status:200,headers:{'Content-Type':'application/json'}});
  const oldUser={id:101,alias:'bootstrap-A',city_id:null,can_supervise:false,city_name:null,timezone:null,today:null};
  const newUser={id:202,alias:'bootstrap-B',city_id:null,can_supervise:false,city_name:null,timezone:null,today:null};
  globalThis.document={querySelector(selector){return selector==='#app'?root:toast;}};
  globalThis.location={pathname:'/',origin:'http://testserver',search:'',hash:''};
  globalThis.window={addEventListener(name,handler){listeners[name]=handler;}};
  globalThis.history={pushState(_a,_b,url){const u=new URL(url,'http://testserver');Object.assign(globalThis.location,{search:u.search,hash:u.hash});},replaceState(_a,_b,url){this.pushState(_a,_b,url);}};
  globalThis.sessionStorage={getItem(key){return storage.get(key)??null;},setItem(key,value){storage.set(key,String(value));},removeItem(key){storage.delete(key);}};
  globalThis.FormData=class{constructor(form){this.form=form;}get(key){return this.form.values[key];}};
  globalThis.fetch=async(url,options={})=>{
   if(url==='/api/v1/auth/me'&&calls++===0){meStarted();await meFetchPromise;return {ok:true,status:200,json:async()=>{meJsonRead();return {user:oldUser};}};}
   if(url==='/api/v1/auth/login'){
    activeSession='bootstrap-B';resolveLoginStarted();
    if(loginDeferred)await loginWait;
    return jsonResponse({user:newUser,access_token:'test-token-B',token_type:'bearer'});
   }
   if(url.startsWith('/api/v1/days/')){
    daySession=activeSession;
    const date=url.split('/').at(-1);
    return jsonResponse({date,version:0,prayers:{fajr:0,dhuhr:0,asr:0,maghrib:0,isha:0},
     sunnah:{fajr_before:0,dhuhr_before:0,dhuhr_after:0,maghrib_after:0,isha_after:0,witr:false},
     dhuhr_before_blocks:[false,false],stats:{completed:0,total:5,home:0,mosque:0,sunnah_rakahs:0,witr:false,
      gold_medals:0,silver_medals:0,percent:0,gold_target:135,silver_target:7},schedule:null});
   }
   throw new Error(`Unexpected API request ${options.method||'GET'} ${url}`);
  };
  try{
   await import(`../frontend/app.js?bootstrap-race=${runId}`);
   await meStartedPromise;
   globalThis.location.hash='#invite=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA';
   await listeners.hashchange();
   assert.ok(root.innerHTML.includes('auth-form'),'invite hash should show auth while /auth/me is pending');
   const form={id:'auth-form',values:{alias:'bootstrap-B',password:'password-for-B'},querySelector(){return {disabled:false,textContent:''};}};
   const login= listeners.submit({target:form,preventDefault(){}});
   if(releaseMeDuringLogin){
    await loginStartedPromise;
    resolveMeFetch();await meParsedPromise;await delay(0);
    assert.ok(!root.innerHTML.includes('bootstrap-A'),'late /auth/me must be ignored while login is pending');
    resolveLogin();
   }
   await login;
   assert.ok(root.innerHTML.includes('bootstrap-B'),'login B should render after authentication');
   if(!releaseMeDuringLogin){resolveMeFetch();await meParsedPromise;await delay(0);}
   assert.ok(root.innerHTML.includes('bootstrap-B'),'stale bootstrap response must not replace login B');
   assert.equal(daySession,'bootstrap-B','post-login day read must use session B');
  }finally{
   for(const [key,descriptor] of Object.entries(originalDescriptors)){
    if(descriptor)Object.defineProperty(globalThis,key,descriptor);else delete globalThis[key];
   }
  }
 };
 await run(false,'complete-first');
 await run(true,'pending-login');
});
