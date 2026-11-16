// Run with NODE_PATH pointing to an installed playwright package. Uses installed Edge.
const { chromium } = require('playwright');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const { createInterface } = require('node:readline');
const assert = require('node:assert/strict');
const path = require('node:path');
const os = require('node:os');
async function server(port=0) {
  const child=spawn('python',['-u','-m','tests.browser_fixture',String(port)],{cwd:path.join(__dirname,'..'),windowsHide:true,stdio:['ignore','pipe','pipe']});
  const exit=once(child,'exit'); let stderr='';child.stderr.on('data',chunk=>stderr+=chunk);
  const lines=createInterface({input:child.stdout});
  const result=await Promise.race([once(lines,'line').then(([line])=>Number(line)),exit.then(()=>{throw Error(stderr)})]);
  return {port:result,stop:async()=>{child.kill();await exit;}};
}
async function launch(port, setup=false) {
  const child=spawn('python',['-u','-m','tests.browser_fixture','--launch',String(port),setup?'setup':'dashboard'],
    {cwd:path.join(__dirname,'..'),windowsHide:true,stdio:['ignore','pipe','pipe']});
  let stdout='',stderr='';child.stdout.on('data',chunk=>stdout+=chunk);child.stderr.on('data',chunk=>stderr+=chunk);
  const timer=setTimeout(()=>child.kill(),15000);
  const [code]=await once(child,'exit');clearTimeout(timer);
  assert.equal(code,0,stderr);
  return stdout.trim() ? stdout.trim().split(/\r?\n/).map(line=>JSON.parse(line).opened) : [];
}
(async()=>{
  let host=await server(), browser;
  try {
    browser=await chromium.launch({channel:'msedge',headless:true});
    const context=await browser.newContext();const page=await context.newPage();
    let configReads=0;page.on('request',request=>{if(request.url().endsWith('/api/config'))configReads++});
    const base=`http://127.0.0.1:${host.port}`;
    const command=async(setup=true)=>{
      const r=await fetch(base+'/api/browser',{method:'POST',headers:{'x-dashboard-token':'browser-fixture-token','content-type':'application/json'},body:JSON.stringify({action:'open',setup})});
      assert.equal(r.status,200);return r.json();
    };
    await page.goto(base+'/settings#token=browser-fixture-token');
    await page.locator('#provider-select').selectOption('local');
    await page.locator('#cfg-local-LOCAL_LLM_MODEL').fill('unsaved-draft');
    await page.waitForTimeout(1200);
    const before=configReads;assert.equal((await command()).present,true);
    await page.waitForFunction(()=>document.getElementById('toasts').textContent.includes('입력 중인 설정'),{timeout:10000});
    assert.ok(configReads>before);assert.equal(await page.locator('#cfg-local-LOCAL_LLM_MODEL').inputValue(),'unsaved-draft');
    const second=await context.newPage();await second.goto(base+'/#token=browser-fixture-token');
    await page.bringToFront();await page.waitForTimeout(1200);await command();await page.waitForTimeout(1500);
    assert.equal(new URL(second.url()).pathname,'/');assert.equal(context.pages().length,2);
    const port=host.port;await host.stop();host=await server(port);
    await page.waitForTimeout(1500);const prior=configReads;assert.equal((await command()).present,true);
    await page.waitForTimeout(1800);assert.ok(configReads>prior);assert.equal(await page.locator('#cfg-local-LOCAL_LLM_MODEL').inputValue(),'unsaved-draft');
    await second.bringToFront();await second.reload();
    await second.locator('#gateway-evidence').waitFor();
    assert.match(await second.locator('#gateway-evidence').textContent(),/running-model/);
    assert.match(await second.locator('#gateway-evidence').textContent(),/saved-model/);
    await second.locator('#gateway-evidence details[data-section="startup"] summary').click();
    await second.evaluate(() => tick());
    assert.equal(await second.locator('#gateway-evidence details[data-section="startup"]').getAttribute('open'), '');
    const denied=await fetch(base+'/api/browser',{method:'POST',headers:{'content-type':'application/json'},body:'{"action":"open"}'});
    assert.equal(denied.status,403);
    await second.screenshot({path:path.join(os.tmpdir(),'openclaude-gateway-monitoring.png'),fullPage:true});
    await page.close();await second.bringToFront();await second.waitForTimeout(1200);
    assert.deepEqual(await launch(host.port),[],'responsive browser acknowledges the real launcher');
    const cdp=await context.newCDPSession(second);
    await cdp.send('Debugger.enable');
    const paused=new Promise(resolve=>cdp.once('Debugger.paused',resolve));
    await cdp.send('Debugger.pause');await paused;
    let opened;
    try { opened=await launch(host.port,true); }
    finally { await cdp.send('Debugger.resume');await cdp.send('Debugger.disable'); }
    assert.equal(opened.length,1,'an unresponsive tab must not suppress opening settings');
    assert.equal(new URL(opened[0]).pathname,'/settings');
    await second.waitForTimeout(1500);
    assert.equal(new URL(second.url()).pathname,'/','a resumed tab must not execute the cancelled navigation');
    const replacement=await context.newPage();await replacement.goto(opened[0]);
    await replacement.locator('#provider-select').waitFor();
    console.log('PASS: settings refresh, draft preservation, one-tab command, restart recovery, runtime model display, launcher acknowledgement, paused-tab fallback and late-command cancellation; Edge headless');
  } finally {await browser?.close();await host.stop();}
})().catch(error=>{console.error(error);process.exitCode=1});
