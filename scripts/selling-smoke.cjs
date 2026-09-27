// Browser verification for split tenders, dues, store credit, partial returns, exchanges and drawer shifts.
// Run against the isolated snapshot server on port 8001 (README → Validation).
const {chromium}=require('playwright-core');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const base=process.env.BASE_URL||'http://127.0.0.1:8001';

(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
 const page=await browser.newPage({viewport:{width:1440,height:1050}});
 const errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 const api=async(path,body)=>page.evaluate(async([path,body])=>{const csrf=document.cookie.split('; ').find(s=>s.startsWith('csrftoken='))?.split('=')[1];const r=await fetch(path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json','X-CSRFToken':csrf}:{},body:body?JSON.stringify(body):undefined});return {status:r.status,data:await r.json()};},[path,body]);
 const noModal=()=>page.waitForFunction(()=>!document.querySelector('.modal'));
 await page.goto(base);
 await page.locator('[name=username]').fill('owner');
 await page.locator('[name=password]').fill(process.env.DEMO_PASSWORD||'AltabDemo!2026');
 await page.getByRole('button',{name:'Sign in to workspace'}).click();
 await page.locator('.sidebar').waitFor();
 fs.mkdirSync('test-results',{recursive:true});
 const state=(await api('/api/state/')).data;
 const product=state.products.find(p=>p.available>5&&p.active&&!p.vat_exempt);
 const customer=state.customers[0];

 // --- Open a drawer shift, then a split-tender cash sale with change ---
 const open=(await api('/api/shifts/')).data.current;
 if(open)await api('/api/shifts/action/',{action:'close',counted_cash:open.expected_cash,note:'Closed by smoke test'});
 await page.evaluate(()=>sessionStorage.removeItem('altab-exchange'));
 await page.goto(base+'/#pos');
 await page.reload();
 await page.locator('.drawer-bar').waitFor();
 await page.locator('[data-sell=shift-open]').click();
 await page.locator('.sell-form[data-kind=shift-open] [name=opening_float]').fill('500');
 await page.locator('.sell-form[data-kind=shift-open] button[type=submit]').click();
 await noModal();
 await page.locator('.drawer-bar.open').waitFor();
 await page.locator('#page-search').fill(product.barcode);
 await page.locator('#page-search').press('Enter');
 await page.locator('.cart-item').waitFor();
 await page.locator('[data-sell=add-tender]').click();
 await page.locator('[data-tender=method][data-index="0"]').selectOption('bKash');
 await page.locator('[data-tender=amount][data-index="0"]').fill('100');
 await page.locator('[data-tender=reference][data-index="0"]').fill('TXN123');
 await page.locator('#cash-received').fill(String(Math.ceil(product.price)+50));
 assert.ok((await page.locator('.cart-total').textContent()).includes('Change'),'change preview shown');
 await page.getByRole('button',{name:'Complete sale',exact:true}).click();
 await page.getByRole('heading',{name:'Another happy customer.'}).waitFor();
 const modalText=await page.locator('.modal').textContent();
 assert.ok(modalText.includes('bKash')&&modalText.includes('Change to give'),'completion modal lists tenders and change');
 const saleId=Number((await page.locator('.modal a[href^="/receipt/"]').getAttribute('href')).split('/')[2]);
 await page.getByRole('button',{name:'Next sale'}).click();
 const receipt=await (await page.request.get(base+`/receipt/${saleId}/`)).text();
 assert.ok(receipt.includes('Paid by bKash')&&receipt.includes('Includes VAT'),'receipt shows tenders and VAT');

 // --- Sale on account for a customer with a credit limit ---
 await api('/api/action/',{action:'customer',id:customer.id,name:customer.name,phone:customer.phone,email:customer.email,address:customer.address,pricing_tier:'retail',credit_limit:'50000'});
 await page.reload();
 await page.locator('#sale-customer').waitFor();
 await page.locator('#page-search').fill(product.barcode);
 await page.locator('#page-search').press('Enter');
 await page.locator('.cart-item').waitFor();
 await page.locator('#sale-customer').selectOption(String(customer.id));
 await page.locator('#sale-payment').selectOption('On account');
 assert.ok((await page.locator('.cart-total').textContent()).includes('Left on account'));
 await page.getByRole('button',{name:'Complete sale',exact:true}).click();
 await page.getByRole('heading',{name:'Another happy customer.'}).waitFor();
 const onAccountId=Number((await page.locator('.modal a[href^="/receipt/"]').getAttribute('href')).split('/')[2]);
 await page.getByRole('button',{name:'Next sale'}).click();
 let orders=(await api('/api/state/')).data.orders;
 assert.equal(orders.find(o=>o.id===onAccountId).due,Number(product.price));

 // --- Partial return with cash refund from order detail ---
 await page.goto(base+'/#orders');
 await page.reload();
 await page.locator(`[data-action="order:${saleId}"]`).first().click();
 await page.locator('[data-sell^="return:"]').click();
 await page.locator('.sell-form[data-kind=return] [name=reason]').fill('Customer changed mind');
 await page.locator('.sell-form[data-kind=return] [name=refund_method]').selectOption('Cash');
 await page.getByRole('button',{name:'Record return',exact:true}).click();
 await page.getByRole('heading',{name:'Return recorded'}).waitFor();
 assert.ok((await page.locator('.modal').textContent()).includes('Refunded now (Cash)'));
 await page.getByRole('button',{name:'Done'}).click();
 await noModal();
 orders=(await api('/api/state/')).data.orders;
 assert.equal(orders.find(o=>o.id===saleId).status,'Returned');

 // --- Returning unpaid on-account goods cancels the debt rather than creating credit ---
 await page.locator(`[data-action="order:${onAccountId}"]`).first().click();
 await page.locator('[data-sell^="exchange:"]').click();
 await page.locator('.sell-form[data-kind=return] [name=reason]').fill('Never paid, returned');
 await page.getByRole('button',{name:'Record return & start exchange'}).click();
 await page.getByRole('heading',{name:'Nothing to carry over'}).waitFor();
 await page.getByRole('button',{name:'Go to point of sale'}).click();
 orders=(await api('/api/state/')).data.orders;
 assert.equal(orders.find(o=>o.id===onAccountId).due,0,'unpaid balance cancelled');

 // --- Exchange: a paid sale becomes exchange credit on the replacement ---
 const paid=(await api('/api/action/',{action:'sale',items:[{product:product.id,quantity:1}],payment:'Cash'})).data.order;
 await page.reload();
 await page.locator(`[data-action="order:${paid.id}"]`).first().click();
 await page.locator('[data-sell^="exchange:"]').click();
 await page.locator('.sell-form[data-kind=return] [name=reason]').fill('Swap for another unit');
 await page.getByRole('button',{name:'Record return & start exchange'}).click();
 await page.locator('.exchange-banner').waitFor();
 await page.locator('#page-search').fill(product.barcode);
 await page.locator('#page-search').press('Enter');
 await page.locator('.cart-item').waitFor();
 assert.ok((await page.locator('.cart-total').textContent()).includes('Exchange credit'));
 await page.getByRole('button',{name:'Complete sale',exact:true}).click();
 await page.getByRole('heading',{name:'Another happy customer.'}).waitFor();
 assert.ok((await page.locator('.modal').textContent()).includes('Exchange credit'));
 await page.getByRole('button',{name:'Next sale'}).click();
 assert.equal(await page.locator('.exchange-banner').count(),0,'exchange context cleared');

 // --- Customers page shows tiers/dues/credit and the account modal ---
 await page.goto(base+'/#customers');
 await page.reload();
 await page.locator('.customer-stats').waitFor();
 await page.locator(`[data-sell="account:${customer.id}"]`).click();
 await page.locator('.sell-form[data-kind=credit]').waitFor();
 await page.locator('.sell-form[data-kind=credit] [name=amount]').fill('250');
 await page.locator('.sell-form[data-kind=credit] [name=reason]').fill('Loyalty thank-you');
 await page.getByRole('button',{name:'Apply adjustment'}).click();
 await noModal();
 assert.equal((await api('/api/state/')).data.customers.find(c=>c.id===customer.id).credit_balance,250);
 await page.screenshot({path:'test-results/customers.png',fullPage:true});

 // --- Close the shift with a counted amount and see the Z report ---
 await page.goto(base+'/#pos');
 await page.reload();
 await page.locator('[data-sell=shift-close]').click();
 await page.locator('.z-report').waitFor();
 const expected=Number((await api('/api/shifts/')).data.current.expected_cash);
 await page.locator('.sell-form[data-kind=shift-close] [name=counted_cash]').fill(String(expected-10));
 assert.ok((await page.locator('#variance-preview').textContent()).includes('-'),'variance preview');
 await page.locator('.sell-form[data-kind=shift-close] button[type=submit]').click();
 await page.getByRole('heading',{name:/Z report/}).waitFor();
 await page.screenshot({path:'test-results/z-report.png',fullPage:true});
 await page.getByRole('button',{name:'Done'}).click();
 await page.goto(base+'/#accounting');
 await page.reload();
 await page.locator('[data-operation=accounting-tab][data-value=drawer]').click();
 await page.locator('[data-sell^="z-report:"]').first().waitFor();

 // --- Settings: VAT and receipt footer ---
 await page.goto(base+'/#settings');
 await page.reload();
 await page.locator('[data-commerce=settings-tab][data-value=sales]').click();
 await page.locator('.sell-form[data-kind=settings] [name=receipt_footer]').fill('Smoke footer text');
 await page.getByRole('button',{name:'Save sales settings'}).click();
 await page.waitForFunction(()=>document.querySelector('#toast')?.classList.contains('show'));
 assert.equal((await api('/api/state/')).data.profile.receipt_footer,'Smoke footer text');

 await page.setViewportSize({width:390,height:844});
 await page.goto(base+'/#pos');
 await page.locator('.drawer-bar').waitFor();
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'POS with drawer bar fits a phone');
 await page.screenshot({path:'test-results/pos-mobile.png',fullPage:true});
 assert.deepEqual(errors,[]);
 console.log('PASS: drawer shift, split tender + change, on-account sale, partial return with cash refund, exchange credit, store credit adjustment, Z report, VAT settings, mobile layout.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
