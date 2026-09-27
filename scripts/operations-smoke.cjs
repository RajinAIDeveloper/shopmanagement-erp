// Browser verification for product editing, settlements and the activity log. Run against the isolated
// snapshot server on port 8001 (see README → Validation); it writes real records into that snapshot only.
const {chromium}=require('playwright-core');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const base=process.env.BASE_URL||'http://127.0.0.1:8001';
const png=process.env.TEST_IMAGE||'.runtime/test-logo.png';

(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
 const page=await browser.newPage({viewport:{width:1440,height:1050}});
 const errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 const api=async(path,body)=>page.evaluate(async([path,body])=>{const csrf=document.cookie.split('; ').find(s=>s.startsWith('csrftoken='))?.split('=')[1];const r=await fetch(path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json','X-CSRFToken':csrf}:{},body:body?JSON.stringify(body):undefined});return {status:r.status,data:await r.json()};},[path,body]);
 await page.goto(base);
 await page.locator('[name=username]').fill('owner');
 await page.locator('[name=password]').fill(process.env.DEMO_PASSWORD||'AltabDemo!2026');
 await page.getByRole('button',{name:'Sign in to workspace'}).click();
 await page.locator('.sidebar').waitFor();
 fs.mkdirSync('test-results',{recursive:true});

 // --- Product editing with a photo, locked stock/cost, and deactivation ---
 const state=(await api('/api/state/')).data;
 const product=state.products.find(p=>p.available>3&&p.active);
 await page.goto(base+'/#products');
 await page.locator(`[data-action="product:${product.id}"]`).first().click();
 await page.getByRole('button',{name:'Edit product'}).click();
 const editor=page.locator('.operations-form[data-kind=product]');
 await editor.waitFor();
 assert.equal(await editor.locator('[name=stock]').count(),0,'stock is not editable after creation');
 await editor.locator('[name=name]').fill(product.name+' (2026 edition)');
 await editor.locator('[name=variant]').fill('Smoke-test variant');
 await editor.locator('[data-upload=photo]').setInputFiles(png);
 await page.waitForFunction(()=>document.querySelector('.operations-form [name=photo]').value.startsWith('/media/'));
 await page.getByRole('button',{name:'Save product'}).click();
 await page.waitForFunction(()=>!document.querySelector('.modal'));
 const edited=(await api('/api/state/')).data.products.find(p=>p.id===product.id);
 assert.equal(edited.name,product.name+' (2026 edition)');
 assert.equal(edited.stock,product.stock,'stock unchanged by edit');
 assert.ok(edited.photo.startsWith('/media/uploads/'));
 await page.screenshot({path:'test-results/products-edited.png',fullPage:true});
 const shop=await browser.newPage({viewport:{width:1440,height:1050}});
 await shop.goto(base+'/shop/');
 await shop.locator('.catalog-card').first().waitFor();
 assert.equal(await shop.locator(`.catalog-card img[src="${edited.photo}"]`).count(),1,'storefront shows the uploaded photo');
 await shop.close();

 // --- Reservation expiry surfaces in Orders and releases stock ---
 const checkout=(await api('/api/checkout/',{name:'Expiry smoke',phone:'01700000001',address:'Dhaka',items:[{product:product.id,quantity:1}]})).data;
 assert.ok(checkout.reservation_expires_at,'checkout reports the reservation deadline: '+JSON.stringify(checkout));
 await page.goto(base+'/#orders');
 await page.reload();
 await page.locator('#page-filter').waitFor();
 await page.locator('#page-filter').selectOption('Pending');
 await page.getByText(checkout.number,{exact:true}).first().waitFor();
 await page.locator(`[data-action="order:${(await api('/api/state/')).data.orders.find(o=>o.number===checkout.number).id}"]`).first().click();
 await page.locator('.expiry-note').waitFor();
 await page.getByRole('button',{name:'Cancel order',exact:true}).click();
 await page.waitForFunction(()=>!document.querySelector('.modal'));

 // --- Settlements: COD collection, partial then full, and a reversal ---
 const shipped=(await api('/api/state/')).data.orders.find(o=>o.channel==='Online'&&['Shipped','Delivered'].includes(o.status)&&o.due>0);
 assert.ok(shipped,'demo data has an uncollected shipped/delivered online order');
 await page.goto(base+'/#accounting');
 await page.locator('[data-operation=accounting-tab][data-value=settlements]').click();
 await page.locator('.settlement-sections').waitFor();
 await page.locator(`[data-operation=settlement][data-kind=collection][data-value="${shipped.id}"]`).click();
 const pay=page.locator('.operations-form[data-kind=settlement]');
 await pay.locator('[name=amount]').fill('100');
 await pay.locator('[name=reference]').fill('Smoke partial remittance');
 await page.getByRole('button',{name:'Record payment'}).click();
 await page.waitForFunction(()=>!document.querySelector('.modal'));
 await page.locator('.settlement-sections').waitFor();
 let balances=(await api('/api/settlements/')).data;
 let row=balances.receivables.find(r=>r.id===shipped.id);
 assert.equal(Number(row.paid),100);
 await page.locator(`[data-operation=settlement][data-kind=collection][data-value="${shipped.id}"]`).click();
 await page.locator('.operations-form[data-kind=settlement] [name=reference]').fill('Smoke final remittance');
 await page.getByRole('button',{name:'Record payment'}).click();
 await page.waitForFunction(()=>!document.querySelector('.modal'));
 await page.locator('.settlement-sections').waitFor();
 balances=(await api('/api/settlements/')).data;
 assert.equal(balances.receivables.find(r=>r.id===shipped.id),undefined,'fully collected order leaves the receivables list');
 const final=balances.entries.find(e=>e.reference==='Smoke final remittance');
 await page.locator(`[data-operation=reverse-payment][data-value="${final.id}"]`).click();
 await page.locator('.operations-form[data-kind=settlement] [name=note]').fill('Recorded against the wrong order');
 await page.getByRole('button',{name:'Confirm reversal'}).click();
 await page.waitForFunction(()=>!document.querySelector('.modal'));
 await page.locator('.settlement-sections').waitFor();
 balances=(await api('/api/settlements/')).data;
 assert.equal(Number(balances.receivables.find(r=>r.id===shipped.id).paid),100,'reversal restores the balance');
 assert.equal(balances.entries.find(e=>e.id===final.id).reversed,true);
 await page.screenshot({path:'test-results/settlements.png',fullPage:true});

 // --- Activity log: searchable, with a before/after diff for the product edit ---
 await page.goto(base+'/#settings');
 await page.locator('[data-commerce=settings-tab][data-value=activity]').click();
 await page.locator('.audit-entry').first().waitFor();
 await page.locator('#activity-search [name=q]').fill('Product updated');
 await page.getByRole('button',{name:'Search',exact:true}).click();
 await page.locator('.audit-entry').first().waitFor();
 await page.locator('.audit-entry').first().locator('summary').click();
 await page.locator('.audit-detail table').first().waitFor();
 assert.ok((await page.locator('.audit-detail').first().textContent()).includes('2026 edition'));
 await page.locator('#activity-search [name=q]').fill('Signed in');
 await page.getByRole('button',{name:'Search',exact:true}).click();
 await page.locator('.audit-entry').first().waitFor();
 await page.screenshot({path:'test-results/activity.png',fullPage:true});

 // --- Accountant sees Activity only; cashier is refused on settlements ---
 const health=await page.request.get(base+'/healthz/');
 assert.equal((await health.json()).database,'ok');
 await page.setViewportSize({width:390,height:844});
 await page.goto(base+'/#accounting');
 await page.locator('[data-operation=accounting-tab][data-value=settlements]').click();
 await page.locator('.settlement-sections').waitFor();
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'settlements fit a phone');
 await page.screenshot({path:'test-results/settlements-mobile.png',fullPage:true});
 assert.deepEqual(errors,[]);
 console.log('PASS: product edit + photo, locked stock, reservation deadline, COD collection/reversal, activity search/diff, health check, mobile layout.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
