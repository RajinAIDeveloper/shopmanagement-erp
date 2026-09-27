const {chromium}=require('playwright-core');
const assert=require('node:assert/strict');
const base=process.env.BASE_URL||'http://127.0.0.1:8001';
(async()=>{const browser=await chromium.launch({headless:true});try{
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>d.accept());
 await page.goto(base);await page.locator('[name=username]').fill('owner');await page.locator('[name=password]').fill(process.env.DEMO_PASSWORD||'AltabDemo!2026');await page.getByRole('button',{name:'Sign in to workspace'}).click();await page.locator('.sidebar').waitFor();
 await page.goto(base+'/#reports');await page.getByRole('heading',{name:'Trial balance',exact:true}).waitFor();
 await page.locator('[data-ledger=tab][data-id=vouchers]').click();await page.locator('[data-ledger=opening]').click();
 const reference='Opening smoke '+Date.now();
 await page.locator('.ledger-form [name=reference]').fill(reference);await page.locator('.ledger-form [name=memo]').fill('Smoke test starting balance');await page.locator('.ledger-form [name=amount]').fill('1000');await page.locator('.ledger-form button[type=submit]').click();
 await page.getByRole('cell',{name:reference,exact:true}).waitFor();
 await page.getByRole('row').filter({has:page.getByRole('cell',{name:reference,exact:true})}).getByRole('button',{name:'Reverse'}).click();
 await page.locator('.ledger-form [name=reference]').fill(reference+' reversal');await page.locator('.ledger-form [name=memo]').fill('Correct smoke balance');await page.locator('.ledger-form button[type=submit]').click();await page.getByRole('cell',{name:reference+' reversal',exact:true}).waitFor();
 await page.locator('[data-ledger=tab][data-id=accounts]').click();await page.getByRole('cell',{name:'Opening equity',exact:true}).waitFor();
 await page.locator('[data-ledger=tab][data-id=statements]').click();await page.screenshot({path:'test-results/accounting-statements.png',fullPage:true});
 await page.setViewportSize({width:390,height:844});await page.screenshot({path:'test-results/accounting-mobile.png',fullPage:true});const overflow=await page.evaluate(()=>[...document.querySelectorAll('*')].filter(e=>e.getBoundingClientRect().right>innerWidth+1).slice(0,15).map(e=>({tag:e.tagName,cls:e.className,width:e.getBoundingClientRect().width})));assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,JSON.stringify(overflow));assert.deepEqual(errors,[]);
 console.log('PASS: dated financial statements, opening balance posting and reversal, chart, mobile layout.');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1);});
