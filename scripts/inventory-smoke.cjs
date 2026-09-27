// Browser verification for locations, per-branch POS stock, transfers, stock counts and multi-line purchase orders.
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
 page.on('dialog',d=>d.accept());
 const api=async(path,body)=>page.evaluate(async([path,body])=>{const csrf=document.cookie.split('; ').find(s=>s.startsWith('csrftoken='))?.split('=')[1];const r=await fetch(path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json','X-CSRFToken':csrf}:{},body:body?JSON.stringify(body):undefined});return {status:r.status,data:await r.json()};},[path,body]);
 const noModal=()=>page.waitForFunction(()=>!document.querySelector('.modal'));
 await page.goto(base);
 await page.locator('[name=username]').fill('owner');
 await page.locator('[name=password]').fill(process.env.DEMO_PASSWORD||'AltabDemo!2026');
 await page.getByRole('button',{name:'Sign in to workspace'}).click();
 await page.locator('.sidebar').waitFor();
 fs.mkdirSync('test-results',{recursive:true});
 await page.evaluate(()=>{localStorage.removeItem('altab-pos-location');sessionStorage.removeItem('altab-exchange');});

 // --- Settings → Locations: add a branch ---
 await page.goto(base+'/#settings');
 await page.reload();
 await page.locator('[data-commerce=settings-tab][data-value=locations]').click();
 await page.locator('[data-inv=new-location]').click();
 const branchName='Smoke branch '+Date.now().toString().slice(-5);
 await page.locator('.inv-form[data-kind=location] [name=name]').fill(branchName);
 await page.locator('.inv-form[data-kind=location] [name=code]').fill('SMK'+Date.now().toString().slice(-4));
 await page.locator('.inv-form[data-kind=location] button[type=submit]').click();
 await noModal();
 await page.locator('td strong',{hasText:branchName}).waitFor();
 let state=(await api('/api/state/')).data;
 const main=state.locations.find(l=>l.pos_default);
 const branch=state.locations.find(l=>l.name===branchName);
 const product=state.products.find(p=>p.active&&(p.levels[String(main.id)]?.available||0)>10);

 // --- Transfer 3 units to the branch, dispatch and receive 2 with 1 written off ---
 await page.goto(base+'/#inventory');
 await page.reload();
 await page.locator('[data-action="inventory-tab:transfers"]').click();
 await page.locator('[data-inv=new-transfer]').click();
 await page.locator('.inv-form[data-kind=transfer] [name=source]').selectOption(String(main.id));
 await page.locator('.inv-form[data-kind=transfer] [name=destination]').selectOption(String(branch.id));
 await page.locator('[data-line=product][data-index="0"]').selectOption(String(product.id));
 await page.locator('[data-line=quantity][data-index="0"]').fill('3');
 await page.locator('.inv-form[data-kind=transfer] button[type=submit]').click();
 await noModal();
 await page.locator('[data-inv^="receive-transfer:"]').first().waitFor();
 const transfer=(await api('/api/inventory/')).data.transfers[0];
 assert.equal(transfer.status,'Dispatched');
 await page.locator(`[data-inv="receive-transfer:${transfer.id}"]`).click();
 await page.locator(`.inv-form[data-kind=transfer_receive] [name="line-${transfer.lines[0].id}"]`).fill('2');
 await page.locator(`.inv-form[data-kind=transfer_receive] [name="lost-${transfer.lines[0].id}"]`).fill('1');
 await page.locator('.inv-form[data-kind=transfer_receive] button[type=submit]').click();
 await noModal();
 state=(await api('/api/state/')).data;
 const moved=state.products.find(p=>p.id===product.id);
 assert.equal(moved.levels[String(branch.id)].quantity,2,'branch received 2');
 assert.equal(moved.levels[String(main.id)].quantity,product.levels[String(main.id)].quantity-3,'source lost 3');
 await page.locator('[data-action="inventory-tab:products"]').click();
 await page.getByText(branchName,{exact:false}).first().waitFor();
 await page.screenshot({path:'test-results/inventory-locations.png',fullPage:true});

 // --- POS sells from the selected branch and blocks when that branch is empty ---
 await page.goto(base+'/#pos');
 await page.reload();
 await page.locator('#pos-location').selectOption(String(branch.id));
 await page.locator('#page-search').fill(product.barcode);
 await page.locator('#page-search').press('Enter');
 await page.locator('.cart-item').waitFor();
 await page.locator(`[data-action="qty:${product.id}:1"]`).click();
 await page.locator(`[data-action="qty:${product.id}:1"]`).click();
 assert.equal(await page.locator('.cart-item .quantity span').textContent(),'2','cannot exceed branch stock');
 await page.getByRole('button',{name:'Complete sale',exact:true}).click();
 await page.getByRole('heading',{name:'Another happy customer.'}).waitFor();
 await page.getByRole('button',{name:'Next sale'}).click();
 state=(await api('/api/state/')).data;
 assert.equal(state.products.find(p=>p.id===product.id).levels[String(branch.id)].quantity,0);
 assert.equal(state.orders[0].location_id,branch.id);
 assert.equal(await page.locator(`[data-action="add:${product.id}"]`).isDisabled(),true,'sold-out at branch');

 // --- Stock count at main: adjust one product by -1 ---
 await page.goto(base+'/#inventory');
 await page.reload();
 await page.locator('[data-action="inventory-tab:counts"]').click();
 await page.locator('[data-inv=new-count]').click();
 await page.locator('.inv-form[data-kind=count_start] [name=location]').selectOption(String(main.id));
 await page.locator('.inv-form[data-kind=count_start] [name=category]').selectOption(product.category);
 await page.locator('.inv-form[data-kind=count_start] button[type=submit]').click();
 await page.locator('.count-sheet').waitFor();
 const sheet=(await api('/api/inventory/')).data.counts[0];
 const detail=(await api(`/api/inventory/?count=${sheet.id}`)).data.count;
 const line=detail.lines.find(l=>l.product===product.id);
 await page.locator(`.count-sheet [name="line-${line.id}"]`).fill(String(line.expected-1));
 await page.locator(`[data-inv="complete-count:${sheet.id}"]`).click();
 await noModal();
 state=(await api('/api/state/')).data;
 assert.equal(state.products.find(p=>p.id===product.id).levels[String(main.id)].quantity,line.expected-1,'count posted a -1 variance');

 // --- Multi-line purchase order, partial receipt, supplier return ---
 await page.goto(base+'/#purchases');
 await page.reload();
 await page.locator('[data-inv=new-purchase]').click();
 await page.locator('.inv-form[data-kind=purchase] [name=location]').selectOption(String(branch.id));
 await page.locator('[data-line=product][data-index="0"]').selectOption(String(product.id));
 await page.locator('[data-line=quantity][data-index="0"]').fill('10');
 await page.locator('[data-line=cost][data-index="0"]').fill('100');
 await page.locator('[data-inv=add-line]').click();
 const other=state.products.find(p=>p.active&&p.id!==product.id);
 await page.locator('[data-line=product][data-index="1"]').selectOption(String(other.id));
 await page.locator('[data-line=quantity][data-index="1"]').fill('5');
 await page.locator('[data-line=cost][data-index="1"]').fill('50');
 await page.locator('.inv-form[data-kind=purchase] button[type=submit]').click();
 await noModal();
 state=(await api('/api/state/')).data;
 const po=state.purchases[0];
 assert.equal((po.lines.length),2);
 await page.locator(`[data-inv="receive-po:${po.id}"]`).click();
 await page.locator(`.inv-form[data-kind=receive] [name="line-${po.lines[0].id}"]`).fill('4');
 await page.locator(`.inv-form[data-kind=receive] [name="line-${po.lines[1].id}"]`).fill('0');
 await page.locator('.inv-form[data-kind=receive] [name=reference]').fill('DN-SMOKE');
 await page.locator('.inv-form[data-kind=receive] button[type=submit]').click();
 await noModal();
 state=(await api('/api/state/')).data;
 const partial=state.purchases.find(p=>p.id===po.id);
 assert.equal(partial.status,'Partially received');
 assert.equal(state.products.find(p=>p.id===product.id).levels[String(branch.id)].quantity,4);
 await page.locator(`[data-inv="return-po:${po.id}"]`).click();
 await page.locator(`.inv-form[data-kind=purchase_return] [name="line-${po.lines[0].id}"]`).fill('1');
 await page.locator('.inv-form[data-kind=purchase_return] [name=reason]').fill('Scratched');
 await page.locator('.inv-form[data-kind=purchase_return] button[type=submit]').click();
 await noModal();
 state=(await api('/api/state/')).data;
 assert.equal(state.purchases.find(p=>p.id===po.id).due,300,'payable = 4×100 − 1×100');
 await page.locator(`[data-inv="po-detail:${po.id}"]`).click();
 await page.getByText('DN-SMOKE',{exact:false}).waitFor();
 await page.screenshot({path:'test-results/purchase-order.png',fullPage:true});
 await page.locator('.modal-actions [data-action=close]').click();

 await page.setViewportSize({width:390,height:844});
 await page.goto(base+'/#inventory');
 await page.locator('.subtabs').waitFor();
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'inventory fits a phone');
 assert.deepEqual(errors,[]);
 console.log('PASS: location admin, transfer with write-off, branch-scoped POS stock, stock count variance, multi-line PO with partial receipt and supplier return, mobile layout.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
