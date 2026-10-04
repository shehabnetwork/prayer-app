// Exercise group flows through the real frontend handlers and real API.
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

test('groups, invitation consent, visibility, readonly member views and stale reads',async()=>{
 const tmp=await mkdtemp(path.join(tmpdir(),'prayer-supervisors-'));
 const port=20000+Math.floor(Math.random()*1000);
 const base=`http://127.0.0.1:${port}`;
 const server=spawn(process.env.PYTHON||path.join(ROOT,'.venv/bin/python'),['-m','uvicorn','backend.main:app','--host','127.0.0.1','--port',String(port)],{cwd:ROOT,env:{...process.env,PRAYER_DB_PATH:path.join(tmp,'test.sqlite3')},stdio:'ignore'});
 const originals={fetch:globalThis.fetch,document:globalThis.document,window:globalThis.window,location:globalThis.location,history:globalThis.history,FormData:globalThis.FormData,sessionStorage:globalThis.sessionStorage};
 let cookie='',registeredToken='',inviteResponse=null,holdResponse=null,authRequestCount=0,inviteMutations=0,lastGroupPatch=null,failGroupPatch=false,failGroupDelete=false,groupDeleteRequests=0;
 const listeners={},fields={},storage=new Map();
 let manualCopySelected=false,copiedLink='';
 const dummyField=()=>({value:'',disabled:false,textContent:'',focus(){},append(){},select(){manualCopySelected=true;copiedLink=this.value;},classList:{toggle(){}}});
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
  const parentGroup=(await request(parent.access_token,'/api/v1/groups',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'parent-private'})})).body.group;
  const parentInvite=(await request(parent.access_token,`/api/v1/groups/${parentGroup.id}/invite-link`,{method:'POST'})).body;
  const secondSupervisor=await createAccount('other-parent','supervisor');
  const secondGroup=(await request(secondSupervisor.access_token,'/api/v1/groups',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'second-private'})})).body.group;
  const secondInvite=(await request(secondSupervisor.access_token,`/api/v1/groups/${secondGroup.id}/invite-link`,{method:'POST'})).body;

  globalThis.document={createElement(){return dummyField();},querySelector(selector){return selector==='#app'?root:toast;}};
  globalThis.location={pathname:'/',origin:base,search:'',hash:`#invite=${parentInvite.token}`};
  globalThis.window={addEventListener(name,handler){listeners[name]=handler;}};
  globalThis.history={pushState(_a,_b,url){const u=new URL(url,base);Object.assign(globalThis.location,{search:u.search,hash:u.hash});},replaceState(_a,_b,url){this.pushState(_a,_b,url);}};
  globalThis.sessionStorage={getItem(key){return storage.get(key)??null;},setItem(key,value){storage.set(key,String(value));},removeItem(key){storage.delete(key);}};
  globalThis.FormData=class{constructor(form){this.form=form;}get(key){return this.form.values[key];}};
  globalThis.fetch=async(url,options={})=>{
   if(url==='/api/v1/auth/login')authRequestCount++;
   if(url.includes('/invites')&&options.method==='POST')inviteMutations++;
   if(/^\/api\/v1\/groups\/[^/]+$/.test(url)&&options.method==='PATCH'){lastGroupPatch=JSON.parse(options.body);if(failGroupPatch){failGroupPatch=false;return new Response(JSON.stringify({detail:'test setting failure'}),{status:503,headers:{'Content-Type':'application/json'}});}}
   if(/^\/api\/v1\/groups\/[^/]+$/.test(url)&&options.method==='DELETE'){groupDeleteRequests++;if(failGroupDelete){failGroupDelete=false;return new Response(JSON.stringify({detail:'test delete failure'}),{status:503,headers:{'Content-Type':'application/json'}});}}
   const headers={...options.headers,Origin:base,...(cookie?{Cookie:cookie}:{})};
   const response=await originals.fetch(base+url,{...options,headers});
   if(url==='/api/v1/auth/register'&&response.ok)registeredToken=(await response.clone().json()).access_token;
   if(url.endsWith('/invite-link')&&response.ok)inviteResponse=await response.clone().json();
   if(holdResponse&&holdResponse.matches(url,options)){const hold=holdResponse;holdResponse=null;hold.started();await hold.promise;}
   const setCookie=response.headers.get('set-cookie');if(setCookie)cookie=setCookie.split(';')[0];
   return response;
  };
  await import('../frontend/app.js');
  for(let i=0;!root.innerHTML.includes('auth-form');i++){assert.ok(i<120,'initial auth render');await delay(20);}
  const click=async dataset=>listeners.click({target:{closest(){return {dataset,disabled:false};}}});
  await click({action:'auth-mode',mode:'register'});
  const childForm={id:'auth-form',values:{alias:'طفل_الأول',display_name:'طفل_الأول',password:password(),account_type:'child'},querySelector(){return dummyField();}};
  await listeners.submit({target:childForm,preventDefault(){}});
  for(let i=0;!root.innerHTML.includes(parent.user.alias);i++){assert.ok(i<100,'invite preview after registration');await delay(20);}
  assert.match(root.innerHTML,/للقراءة فقط/);
  assert.doesNotMatch(root.innerHTML,/انضممت إلى المجموعة/);
  await click({action:'accept-invite'});
  for(let i=0;!root.innerHTML.includes('مجموعاتي');i++){assert.ok(i<100,'accepted invite returns to account');await delay(20);}
  let relationships=await request(registeredToken,'/api/v1/groups');
  assert.deepEqual(relationships.body.groups.map(item=>item.id),[parentGroup.id]);
  assert.doesNotMatch(root.innerHTML,/data-action="select-member"/,'private member cannot browse records');
  assert.match(root.innerHTML,/role="switch"[^>]*disabled/,'member switch is readonly');
  await click({action:'toggle-group-sharing'});
  assert.equal(lastGroupPatch,null,'member cannot patch visibility');
  const secondAcceptance=await request(registeredToken,'/api/v1/group-invites/accept',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:secondInvite.token,request_key:randomBytes(20).toString('base64url')})});
  assert.equal(secondAcceptance.status,200,JSON.stringify(secondAcceptance.body));
  await click({action:'refresh-management'});
  assert.match(root.innerHTML,/second-private/);
  await click({action:'select-group',id:String(parentGroup.id)});
  await click({action:'leave-group'});
  relationships=await request(registeredToken,'/api/v1/groups');
  assert.deepEqual(relationships.body.groups.map(item=>item.id),[secondGroup.id]);
  // Ordinary accounts create groups without enabling a role.
  assert.doesNotMatch(root.innerHTML,/group-create-form|<details/);
  await click({action:'new-group'});
  assert.match(root.innerHTML,/role="dialog"/);
  await listeners.submit({target:{id:'group-create-form',values:{name:'my-group'},querySelector(){return dummyField();}},preventDefault(){}});
  assert.match(root.innerHTML,/my-group/);
  const ownGroups=(await request(registeredToken,'/api/v1/groups')).body.groups;
  const ownGroup=ownGroups.find(g=>g.name==='my-group');
  assert.equal(ownGroup.members_can_view_records,false);
  const inviteMutationsBeforeCopy=inviteMutations;
  await click({action:'copy-invite'});
  assert.equal(inviteMutations,inviteMutationsBeforeCopy,'copying with no raw secret must not create or rotate invitations');
  assert.doesNotMatch(root.innerHTML,/<details|group-create-form/);
  const ownInviteToken=inviteResponse.token;
  assert.ok(inviteResponse?.token,`UI should create a reusable supervisor link: ${root.innerHTML.slice(-800)}`);
  await click({action:'copy-invite'});
  assert.ok(manualCopySelected,'clipboard fallback should select the readonly link');
  assert.match(toast.textContent,/انسخه يدويًا/);
  assert.equal(copiedLink,`${base}/#invite=${ownInviteToken}`);
  const children=[];
  for(const alias of ['دفتر_ألف','دفتر_باء']){
   const child=await createAccount(alias);
   const accepted=await request(child.access_token,'/api/v1/group-invites/accept',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:inviteResponse.token,request_key:randomBytes(20).toString('base64url')})});
   assert.equal(accepted.status,200,JSON.stringify(accepted.body));children.push({...child.user,access_token:child.access_token,password:child.password});
  }
  const today=new Date().toISOString().slice(0,10);
  // Seed one day for the first supervised child using its issued account token.
  const seed=await request(children[0].access_token,`/api/v1/days/${today}/prayers/fajr`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({state:2})});
  assert.equal(seed.status,200,JSON.stringify(seed.body));
  await click({action:'refresh-management'});
  assert.ok(root.innerHTML.includes('دفتر_ألف')&&root.innerHTML.includes('دفتر_باء'));

  const readPeer=`/api/v1/groups/${ownGroup.id}/members/${children[1].id}/days/${today}`;
  assert.equal((await request(children[0].access_token,readPeer)).status,403);
  assert.match(root.innerHTML,/class="group-card-header"/);
  assert.match(root.innerHTML,/class="member-admin">المدير/);
  assert.match(root.innerHTML,/role="switch"[^>]*aria-checked="false"/);
  failGroupPatch=true;
  await click({action:'toggle-group-sharing'});
  assert.match(root.innerHTML,/role="switch"[^>]*aria-checked="false"/,'failed setting retains server visibility');
  assert.match(root.innerHTML,/role="alert">test setting failure/);
  await click({action:'toggle-group-sharing'});
  assert.deepEqual(lastGroupPatch,{members_can_view_records:true},'switch sends only the visibility setting');
  assert.match(root.innerHTML,/role="switch"[^>]*aria-checked="true"/);
  assert.equal((await request(children[0].access_token,readPeer)).status,200);
  await click({action:'edit-group'});
  assert.match(root.innerHTML,/id="group-settings-form"/);
  await listeners.submit({target:{id:'group-settings-form',values:{name:'my-group'},querySelector(){return dummyField();}},preventDefault(){}});
  assert.equal((await request(children[0].access_token,readPeer)).status,200,'editing the name preserves shared visibility');
  assert.doesNotMatch(root.innerHTML,/id="group-settings-form"/);
  await click({action:'toggle-group-sharing'});
  assert.equal((await request(children[0].access_token,readPeer)).status,403);

  // Daily and achievement pages for a supervised child show compact read-only records.
  await click({action:'select-member',id:String(children[0].id)});
  assert.match(root.innerHTML,/دفتر دفتر_ألف/);
  assert.match(root.innerHTML,/للقراءة فقط/);
  assert.match(root.innerHTML,/class="report-table"/);
  assert.doesNotMatch(root.innerHTML,/data-action="(?:prayer|sunnah|witr)"/);
  assert.match(root.innerHTML,/دفتر العضو للقراءة فقط/);
  await click({action:'view',view:'achievements'});
  assert.match(root.innerHTML,/إنجازاتي/);
  assert.match(root.innerHTML,/الميداليات الذهبية/);
  assert.match(root.innerHTML,/دفتر دفتر_ألف/);

  // A late day read for child A must not replace the selected child's day for child B.
  let releaseA,startedA;const startedAPromise=new Promise(resolve=>startedA=resolve);
  holdResponse={matches:url=>url.includes(`/groups/${ownGroup.id}/members/${children[0].id}/days/`),promise:new Promise(resolve=>releaseA=resolve),started:startedA};
  const staleChildRead=click({action:'select-member',id:String(children[0].id)});
  await startedAPromise;
  await click({action:'select-member',id:String(children[1].id)});
  assert.match(root.innerHTML,/دفتر دفتر_باء/);
  releaseA();await staleChildRead;
  assert.match(root.innerHTML,/دفتر دفتر_باء/,'late read must not change selected child context');

  // Changing groups cancels an in-flight member record from the old group.
  let releaseGroup,startedGroup;const startedGroupPromise=new Promise(r=>startedGroup=r);
  holdResponse={matches:url=>url.includes(`/groups/${ownGroup.id}/members/${children[0].id}/days/`),promise:new Promise(r=>releaseGroup=r),started:startedGroup};
  const staleGroupRead=click({action:'select-member',id:String(children[0].id)});
  await startedGroupPromise;
  await click({action:'select-group',id:String(secondGroup.id)});
  releaseGroup();await staleGroupRead;
  assert.match(root.innerHTML,/second-private/);
  assert.doesNotMatch(root.innerHTML,/دفتر دفتر_ألف|class="report-table"/);
  await click({action:'select-group',id:String(ownGroup.id)});
  const existingInvite=(await request(registeredToken,`/api/v1/groups/${ownGroup.id}/invites`)).body.invites.find(invite=>invite.revoked_at==null);
  const mutationsBeforeLostCopy=inviteMutations;
  await click({action:'copy-invite'});
  assert.equal(inviteMutations,mutationsBeforeLostCopy,'lost raw secret must not rotate an active invite');
  assert.equal((await request(registeredToken,`/api/v1/groups/${ownGroup.id}/invites`)).body.invites.find(invite=>invite.revoked_at==null).id,existingInvite.id);
  assert.doesNotMatch(root.innerHTML,/<details/);
  assert.equal((await request(registeredToken,`/api/v1/groups/${ownGroup.id}/invite-link`,{method:'POST'})).body.token,ownInviteToken);
  await click({action:'refresh-management'});
  assert.equal((await request(registeredToken,`/api/v1/groups/${ownGroup.id}/invite-link`,{method:'POST'})).body.token,ownInviteToken);

  const adminControl=(await request(null,'/api/v1/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({alias:childForm.values.alias,password:childForm.values.password})})).body.access_token;
  await request(adminControl,`/api/v1/groups/${ownGroup.id}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({members_can_view_records:true})});

  // Likewise, a read from the previous session must not leak after logout and login.
  let releaseLogout,startedLogout;const startedLogoutPromise=new Promise(resolve=>startedLogout=resolve);
  holdResponse={matches:url=>url.includes(`/groups/${ownGroup.id}/members/${children[1].id}/days/`),promise:new Promise(resolve=>releaseLogout=resolve),started:startedLogout};
  const staleLogoutRead=click({action:'select-member',id:String(children[1].id)});
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

  // Logging in again retrieves the original link, without creating or rotating it.
  await click({action:'logout'});
  await listeners.submit({target:{id:'auth-form',values:childForm.values,querySelector(){return dummyField();}},preventDefault(){}});
  await click({action:'view',view:'groups'});
  await click({action:'select-group',id:String(ownGroup.id)});
  await click({action:'copy-invite'});
  assert.equal(copiedLink,`${base}/#invite=${ownInviteToken}`);

  // A shared-group member can browse peers; restricting visibility clears a cached report.
  await click({action:'logout'});
  await listeners.submit({target:{id:'auth-form',values:{alias:children[0].alias,password:children[0].password},querySelector(){return dummyField();}},preventDefault(){}});
  await click({action:'view',view:'groups'});
  assert.match(root.innerHTML,/data-action="select-member"/);
  assert.doesNotMatch(root.innerHTML,/group-settings-form|data-action="remove-member"|data-action="delete-group"/);
  await click({action:'view',view:'report'});
  assert.match(root.innerHTML,/دفتر_باء/);
  const restricted=await request(adminControl,`/api/v1/groups/${ownGroup.id}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({members_can_view_records:false})});
  assert.equal(restricted.status,200);
  await click({action:'refresh-management'});
  assert.doesNotMatch(root.innerHTML,/class="report-table"|data-view="report"|data-action="select-member"/);
  assert.match(root.innerHTML,/المدير فقط يستطيع قراءة سجلات الأعضاء/);

  // Group deletion requires explicit confirmation, preserves diaries and rejects late group data.
  await click({action:'logout'});
  await listeners.submit({target:{id:'auth-form',values:childForm.values,querySelector(){return dummyField();}},preventDefault(){}});
  await click({action:'view',view:'groups'});
  await click({action:'select-group',id:String(ownGroup.id)});
  assert.match(root.innerHTML,/data-action="delete-group"/);
  await click({action:'delete-group'});
  assert.match(root.innerHTML,/role="dialog"[^>]*aria-labelledby="group-delete-title"/);
  assert.match(root.innerHTML,/تبقى حسابات الأعضاء وسجلات صلاتهم الشخصية محفوظة/);
  assert.equal(groupDeleteRequests,0,'opening confirmation must not delete');
  await click({action:'cancel-delete-group'});
  await click({action:'confirm-delete-group'});
  assert.equal(groupDeleteRequests,0,'cancel prevents stale confirmation actions');
  await click({action:'delete-group'});
  failGroupDelete=true;
  await click({action:'confirm-delete-group'});
  assert.match(root.innerHTML,/role="alert">test delete failure/);
  assert.match(root.innerHTML,/id="group-delete-title"/,'failed deletion remains retryable');
  assert.equal((await request(adminControl,`/api/v1/groups/${ownGroup.id}`)).status,200);
  await click({action:'cancel-delete-group'});
  let releaseDeletedList,startedDeletedList;
  const deletedListStarted=new Promise(resolve=>startedDeletedList=resolve);
  holdResponse={matches:url=>url==='/api/v1/groups',promise:new Promise(resolve=>releaseDeletedList=resolve),started:startedDeletedList};
  const staleDeletedList=click({action:'refresh-management'});
  await deletedListStarted;
  await click({action:'delete-group'});
  await click({action:'confirm-delete-group'});
  releaseDeletedList();await staleDeletedList;
  assert.equal(groupDeleteRequests,2,'confirmed deletion sends one request per attempt');
  assert.match(root.innerHTML,/second-private/,'remaining group selected');
  assert.doesNotMatch(root.innerHTML,/my-group|دفتر_ألف|دفتر_باء|data-action="copy-invite"|id="group-delete-title"/,'deleted group/member/invitation caches remain cleared after stale refresh');
  assert.equal((await request(adminControl,`/api/v1/groups/${ownGroup.id}/members/${children[0].id}/days/${today}`)).status,403);
  assert.equal((await request(children[0].access_token,`/api/v1/days/${today}`)).body.prayers.fajr,2,'personal prayer remains');
  assert.equal((await request(null,'/api/v1/group-invites/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:ownInviteToken})})).status,404);

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
  globalThis.document={createElement(){return dummyField();},querySelector(selector){return selector==='#app'?root:toast;}};
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
