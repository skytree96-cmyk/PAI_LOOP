"""Run the entry page's actual script without any business API or storage access."""
from pathlib import Path
import subprocess

import pytest


SOURCE = Path(__file__).parents[1] / "src/pai_loop/static/login.js"
HARNESS = r"""
const assert=require('node:assert/strict'),vm=require('node:vm');
const events={},elements={},requests=[],windowEvents={};
for(const id of ['entryLoginForm','entryUsername','entryPassword','entryLoginSubmit','entryLoginStatus','entryLoginRetry']){
 elements[id]={value:'',disabled:true,hidden:true,dataset:{},textContent:'',reportValidity:()=>true,
  addEventListener(name,fn){events[id+':'+name]=fn;}};
}
let reloads=0;
const context=vm.createContext({AbortController,setTimeout,clearTimeout,JSON,
 document:{getElementById:id=>elements[id]},
 window:{location:{href:'https://syn.invalid/notices?notice=SYN-N',reload(){reloads++;}},
  addEventListener(name,fn){windowEvents[name]=fn;},
  localStorage:{setItem(){throw Error('No credential storage');}},
  sessionStorage:{setItem(){throw Error('No credential storage');}}},
 fetch(path,options){return new Promise((resolve,reject)=>requests.push({path,options,resolve,reject}));}});
vm.runInContext(require('node:fs').readFileSync(0,'utf8'),context);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const reply=async(index,status,payload)=>{requests[index].resolve({ok:status>=200&&status<300,status,json:async()=>payload});await flush();};
const submit=()=>events['entryLoginForm:submit']({preventDefault(){}});
const state={enabled:true,authenticated:false};
const active={enabled:true,authenticated:true,account:{id:7}};
(async()=>{
 assert.equal(requests.length,1);assert.equal(requests[0].path,'/api/v1/accounts/me');
 assert.equal(requests[0].options.credentials,'same-origin');assert.equal(requests[0].options.cache,'no-store');
 assert.equal(elements.entryLoginSubmit.disabled,true);
 __TEST__
 assert.ok(requests.every(r=>/^\/api\/v1\/accounts\/(me|login)$/.test(r.path)));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""


@pytest.mark.parametrize("scenario", [
    r"""
    await reply(0,200,state);assert.equal(elements.entryLoginSubmit.disabled,false);
    elements.entryUsername.value=' SYN_ACCOUNT ';elements.entryPassword.value='SYN-password';
    const pending=submit();await flush();assert.equal(requests.length,2);
    assert.equal(requests[1].path,'/api/v1/accounts/login');
    assert.deepEqual(JSON.parse(requests[1].options.body),{username:'SYN_ACCOUNT',password:'SYN-password'});
    assert.deepEqual(Object.keys(requests[1].options.headers).sort(),['Accept','Content-Type']);
    await submit();assert.equal(requests.length,2);
    await reply(1,200,active);await pending;assert.equal(reloads,1);
    assert.equal(elements.entryPassword.value,'');
    assert.equal(context.window.location.href,'https://syn.invalid/notices?notice=SYN-N');
    """,
    r"""
    await reply(0,200,{enabled:false,authenticated:false});
    assert.equal(elements.entryLoginSubmit.disabled,true);assert.equal(elements.entryLoginRetry.hidden,false);
    await submit();assert.equal(requests.length,1);assert.equal(reloads,0);
    """,
    r"""
    await reply(0,401,{});assert.equal(elements.entryLoginSubmit.disabled,false);
    elements.entryPassword.value='SYN-password';const pending=submit();await flush();
    requests[1].reject(Error('SYN ambiguous network result'));await pending;
    assert.equal(elements.entryPassword.value,'');assert.equal(elements.entryLoginSubmit.disabled,true);
    assert.equal(elements.entryLoginRetry.hidden,false);await submit();assert.equal(requests.length,2);
    const retry=events['entryLoginRetry:click']();await flush();
    assert.equal(requests[2].path,'/api/v1/accounts/me');await reply(2,200,active);await retry;
    assert.equal(requests.filter(r=>r.options.method==='POST').length,1);assert.equal(reloads,1);
    """,
    r"""
    await reply(0,200,state);elements.entryPassword.value='SYN-invalid';
    const pending=submit();await flush();await reply(1,401,{});await pending;
    assert.equal(elements.entryLoginSubmit.disabled,false);assert.equal(elements.entryPassword.value,'');
    assert.equal(reloads,0);
    """,
    r"""
    await reply(0,200,{enabled:true,authenticated:true,account:null});
    assert.equal(elements.entryLoginSubmit.disabled,true);assert.equal(elements.entryLoginRetry.hidden,false);
    assert.equal(reloads,0);
    """,
    r"""
    await reply(0,200,state);windowEvents.pageshow({persisted:true});await flush();
    assert.equal(requests[1].path,'/api/v1/accounts/me');assert.equal(elements.entryLoginSubmit.disabled,true);
    await reply(1,200,active);assert.equal(reloads,1);
    """,
])
def test_entry_login_script(scenario):
    result = subprocess.run(["node", "-e", HARNESS.replace("__TEST__", scenario)],
                            input=SOURCE.read_text(encoding="utf-8"), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
