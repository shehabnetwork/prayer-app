// Real API and frontend handlers; semantic markup, without browser rendering.
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

test('display names are separate from login usernames across account, groups, invitations and reports',async()=>{
 const tmp=await mkdtemp(path.join(tmpdir(),'prayer-display-name-'));
 const base=`http://127.0.0.1:${23000+Math.floor(Math.random()*1000)}`;
 const server=spawn(process.env.PYTHON||path.join(ROOT,'.venv/bin/python'),['-m','uvicorn','backend.main:app','--host','127.0.0.1','--port',new URL(base).port],{cwd:ROOT,env:{...process.env,PRAYER_DB_PATH:path.join(tmp,'test.sqlite3')},stdio:'ignore'});
 const keys=['fetch','document','window','location','history','FormData','sessionStorage'];
 const originals=Object.fromEntries(keys.map(key=>[key,globalThis[key]]));
 const listeners={},storage=new Map(),requests=[];
 const dummy=()=>({disabled:false,value:'',textContent:'',hidden:true,focus(){},classList:{toggle(){}}});
 const root={innerHTML:'',addEventListener(name,handler){listeners[name]=handler;},querySelector(){return dummy();}},toast=dummy();
 let cookie='';
 const request=async(token,url,body)=>{
  const response=await originals.fetch(base+'/api/v1'+url,{method:body?'POST':'GET',headers:{Origin:base,'Content-Type':'application/json',...(token?{Authorization:`Bearer ${token}`}:{})},...(body?{body:JSON.stringify(body)}:{})});
  assert.ok(response.ok,await response.clone().text());return response.json();
 };
 const click=async dataset=>listeners.click({target:{closest(){return {dataset,disabled:false};}}});
 const submit=async(id,values)=>listeners.submit({preventDefault(){},target:{id,values,querySelector(){return dummy();}}});
 const escaped='نور &lt;img src=x onerror=&quot;evil()&quot;&gt; &amp; &quot;بطل&quot;';
 const name='نور <img src=x onerror="evil()"> & "بطل"';
 try{
  for(let i=0;i<120;i++){try{if((await originals.fetch(base+'/api/v1/health')).ok)break;}catch{}assert.ok(i<119,'server starts');await delay(25);}
  const admin=await request(null,'/auth/register',{alias:'admin-login',display_name:'مدير النور',password:randomBytes(24).toString('base64url')});
  const group=(await request(admin.access_token,'/groups',{name:'مجموعة النور',members_can_view_records:true})).group;
  const invite=await request(admin.access_token,`/groups/${group.id}/invite-link`,{});
  globalThis.document={querySelector(selector){return selector==='#app'?root:toast;}};
  globalThis.window={addEventListener(name,handler){listeners[name]=handler;}};
  globalThis.location={pathname:'/',origin:base,search:'',hash:`#invite=${invite.token}`};
  globalThis.history={pushState(_a,_b,url){const u=new URL(url,base);Object.assign(globalThis.location,{search:u.search,hash:u.hash});},replaceState(_a,_b,url){this.pushState(_a,_b,url);}};
  globalThis.sessionStorage={getItem(key){return storage.get(key)??null;},setItem(key,value){storage.set(key,String(value));},removeItem(key){storage.delete(key);}};
  globalThis.FormData=class{constructor(form){this.form=form;}get(key){return this.form.values[key];}};
  globalThis.fetch=async(url,options={})=>{
   if(options.body)requests.push({url,body:JSON.parse(options.body)});
   const response=await originals.fetch(base+url,{...options,headers:{...options.headers,Origin:base,...(cookie?{Cookie:cookie}:{})}});
   const setCookie=response.headers.get('set-cookie');if(setCookie)cookie=setCookie.split(';')[0];return response;
  };
  await import('../frontend/app.js');
  for(let i=0;!root.innerHTML.includes('auth-form');i++){assert.ok(i<120);await delay(20);}
  assert.match(root.innerHTML,/اسم المستخدم/);assert.doesNotMatch(root.innerHTML,/name="display_name"/);
  await click({action:'auth-mode',mode:'register'});
  assert.match(root.innerHTML,/name="display_name"[^>]*minlength="1"[^>]*maxlength="80"[^>]*required/);
  await submit('auth-form',{alias:'member-login',password:'random-secret-123'});
  assert.match(root.innerHTML,/اختر اسمًا معروضًا/);
  assert.equal(requests.filter(r=>r.url.endsWith('/auth/register')).length,0);
  const password=randomBytes(24).toString('base64url');
  await submit('auth-form',{alias:'member-login',display_name:'  '+name+'  ',password});
  assert.equal(requests.find(r=>r.url.endsWith('/auth/register')).body.display_name,name);
  assert.match(root.innerHTML,/المدير: مدير النور/);assert.doesNotMatch(root.innerHTML,/admin-login/);
  assert.ok(root.innerHTML.includes(escaped));assert.doesNotMatch(root.innerHTML,/<img src=x/);
  await click({action:'accept-invite'});
  assert.ok(root.innerHTML.includes(escaped));assert.match(root.innerHTML,/مدير النور/);assert.doesNotMatch(root.innerHTML,/member-login|admin-login/);
  assert.ok(root.innerHTML.includes('class="member-avatar" aria-hidden="true">ن&lt;'));
  await click({action:'view',view:'report'});
  assert.ok(root.innerHTML.includes(escaped));assert.match(root.innerHTML,/مدير النور/);assert.doesNotMatch(root.innerHTML,/member-login|admin-login/);
  const me=await (await globalThis.fetch('/api/v1/auth/me')).json();
  await click({action:'report-member',id:String(me.user.id)});
  assert.ok(root.innerHTML.includes(`دفتر ${escaped} — للقراءة فقط`));
  assert.ok(root.innerHTML.includes(`سجل ${escaped} —`));
  await click({action:'view',view:'account'});
  assert.match(root.innerHTML,/اسم المستخدم: <strong dir="auto">member-login/);assert.match(root.innerHTML,/id="city-form"/);
  assert.ok(root.innerHTML.includes(`value="${escaped}"`));
  await submit('display-name-form',{display_name:'  بطل جديد  '});
  assert.match(root.innerHTML,/دفتر بطل جديد الصغير/);
  assert.deepEqual(requests.find(r=>r.url==='/api/v1/account').body,{display_name:'بطل جديد'});
  await click({action:'logout'});
  assert.doesNotMatch(root.innerHTML,/name="display_name"/);
  await submit('auth-form',{alias:'member-login',password,display_name:'ignored'});
  assert.deepEqual(requests.find(r=>r.url.endsWith('/auth/login')).body,{alias:'member-login',password});
  assert.match(root.innerHTML,/دفتر بطل جديد الصغير/);
  await click({action:'view',view:'groups'});assert.match(root.innerHTML,/بطل جديد/);assert.doesNotMatch(root.innerHTML,/member-login/);
  await click({action:'view',view:'report'});assert.match(root.innerHTML,/بطل جديد/);
 }finally{
  for(const key of keys){if(originals[key]===undefined)delete globalThis[key];else globalThis[key]=originals[key];}
  if(server.exitCode===null){server.kill('SIGTERM');await new Promise(resolve=>server.once('exit',resolve));}await rm(tmp,{recursive:true,force:true});
 }
});
