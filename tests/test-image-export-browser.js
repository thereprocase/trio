/* Real browser download test; optional existing Playwright installation. */
'use strict';
const fs=require('fs'), http=require('http'), path=require('path'), assert=require('assert');
let chromium;
try { ({chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright')); }
catch (_) { console.log('SKIP: set PLAYWRIGHT_MODULE to an existing Playwright installation'); process.exit(0); }
const root=path.join(__dirname,'..','server');
const read=n=>fs.readFileSync(path.join(root,n),'utf8');
const source=read('web/js/13-image-export.js');
const png='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=';
const fixture=`<!doctype html><title>Download test</title><p id="words">Select these words normally.</p>
<svg xmlns="http://www.w3.org/2000/svg" width="320" height="160"><rect width="320" height="160" fill="white"/><text x="10" y="50">Wiring diagram</text><image href="${png}" x="180" y="20" width="80" height="80"/></svg>
<img alt="Raster diagram" src="${png}"><canvas width="320" height="160"></canvas>
<script>${source};TrioImageExport.enhance(document)</script>`;
const csp="sandbox allow-scripts allow-downloads; default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; connect-src 'none'; frame-ancestors 'self'";
const app=`<!doctype html><div id="messages"></div><script>
window.Trio={state:{operator:{id:'owner'},readOnly:false},api:{},events:new EventTarget(),actions:{},avatarTone:()=> 'eucalyptus'};
${['web/js/06-core.js','web/js/09-time.js','web/js/10-markdown.js','web/js/11-conversation.js','web/js/13-image-export.js'].map(read).join('\n')}
Trio.conversation.upsert({id:7,member_id:'agent',content:'Select these words normally.'});
</script>`;
const server=http.createServer((req,res)=>{
  res.writeHead(200,{'Content-Type':'text/html',...(req.url==='/page'?{'Content-Security-Policy':csp}:{})});
  res.end(req.url==='/page'?fixture:req.url==='/app'?app:'<iframe title="preview" sandbox="allow-scripts allow-downloads" src="/page"></iframe>');
});
(async()=>{
  await new Promise(r=>server.listen(0,'127.0.0.1',r)); let browser;
  try {
    browser=await chromium.launch({executablePath:process.env.CHROMIUM_BIN || '/usr/bin/chromium',headless:true,args:['--no-sandbox']});
    const page=await browser.newPage({acceptDownloads:true,viewport:{width:390,height:844}});
    const base='http://127.0.0.1:'+server.address().port;
    await page.goto(base+'/page');
    assert.strictEqual(await page.getByRole('button',{name:'Download JPEG',exact:true}).count(),3);
    for(let i=0;i<3;i++) {
      const wait=page.waitForEvent('download'); await page.getByRole('button').nth(i).click(); const d=await wait;
      assert(d.suggestedFilename().endsWith('.jpg')); assert.strictEqual(await d.failure(),null);
      const bytes=fs.readFileSync(await d.path()); assert(bytes.subarray(0,3).equals(Buffer.from([255,216,255])));
    }
    assert.strictEqual(await page.evaluate(()=>{try{return localStorage.length}catch(e){return e.name}}),'SecurityError');
    await page.goto(base+'/preview');
    const wait=page.waitForEvent('download'); await page.frameLocator('iframe').getByRole('button',{name:'Download JPEG',exact:true}).first().click(); assert.strictEqual(await (await wait).failure(),null);
    await page.goto(base+'/app');
    const result=await page.evaluate(()=>{
      const body=document.querySelector('.message-body');const range=document.createRange();range.selectNodeContents(body);
      getSelection().removeAllRanges();getSelection().addRange(range);
      const ev=new MouseEvent('contextmenu',{bubbles:true,cancelable:true});body.dispatchEvent(ev);
      return {selected:getSelection().toString(),prevented:ev.defaultPrevented,actions:!!document.querySelector('.message-more')};
    });
    assert(result.selected.includes('Select these words')); assert.strictEqual(result.prevented,false); assert(result.actions);
    console.log('PASS: SVG/raster/canvas JPEG bytes, opaque sandbox, preview download, mobile text selection/native context menu');
  } finally { await browser?.close(); await new Promise(r=>server.close(r)); }
})().catch(e=>{console.error(e);process.exitCode=1});
