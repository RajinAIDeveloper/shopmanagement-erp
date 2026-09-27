// ESC/POS receipt printing and cash-drawer kick over Web Serial (Chrome/Edge). Everything here is optional:
// the browser print dialog keeps working on devices without serial access.
window.hardware=(()=>{
  let port=null, writer=null;
  const ESC=0x1b, GS=0x1d;
  const encoder=new TextEncoder();
  const supported=()=>'serial' in navigator;
  const connected=()=>!!writer;
  const columns=()=>(localStorage.getItem('altab-paper')||'80')==='58'?32:48;
  const bytes=(...parts)=>{const chunks=parts.map(p=>typeof p==='string'?encoder.encode(p):Uint8Array.from(p));const out=new Uint8Array(chunks.reduce((s,c)=>s+c.length,0));let offset=0;for(const c of chunks){out.set(c,offset);offset+=c.length;}return out;};
  const ascii=s=>String(s??'').replace(/৳/g,'Tk ').replace(/[^\x20-\x7e]/g,'?');
  const pad=(left,right)=>{const width=columns();left=ascii(left);right=ascii(right);const space=Math.max(1,width-left.length-right.length);return (left.length+right.length>=width?left.slice(0,width-right.length-1)+' ':left+' '.repeat(space))+right+'\n';};
  const center=s=>{const width=columns();s=ascii(s);const gap=Math.max(0,Math.floor((width-s.length)/2));return ' '.repeat(gap)+s.slice(0,width)+'\n';};
  const money=v=>'Tk '+Number(v||0).toFixed(2);
  async function connect(){
    if(!supported())throw Error('This browser cannot access serial printers. Use Chrome or Edge.');
    if(port){try{await disconnect();}catch{}}
    port=await navigator.serial.requestPort();
    await port.open({baudRate:Number(localStorage.getItem('altab-printer-baud')||9600)});
    writer=port.writable.getWriter();
    port.addEventListener('disconnect',()=>{writer=null;port=null;});
    return true;
  }
  async function disconnect(){if(writer){writer.releaseLock();writer=null;}if(port){await port.close();port=null;}}
  async function write(data){if(!writer)throw Error('Printer not connected. Connect it in Settings → Sales & VAT.');await writer.write(data);}
  const init=()=>bytes([ESC,0x40]);
  const align=n=>bytes([ESC,0x61,n]);
  const bold=on=>bytes([ESC,0x45,on?1:0]);
  const cut=()=>bytes([ESC,0x64,4],[GS,0x56,66,0]);
  async function openDrawer(){await write(bytes([ESC,0x70,0x00,0x19,0xfa]));}
  async function printReceipt(order,profile){
    const lines=[init(),align(1),bold(true),center(profile?.name||'Receipt'),bold(false)];
    if(profile?.address)lines.push(center(profile.address));
    if(profile?.phone)lines.push(center(profile.phone));
    if(profile?.vat_registration)lines.push(center('BIN '+profile.vat_registration));
    lines.push(center('-'.repeat(columns())),align(0));
    lines.push(pad('Receipt',order.number),pad('Date',new Date(order.date).toLocaleString('en-GB',{dateStyle:'short',timeStyle:'short'})),pad('Customer',order.customer||'Walk-in'));
    lines.push(center('-'.repeat(columns())));
    for(const l of order.items){lines.push(ascii(l.name).slice(0,columns())+'\n',pad(`  ${l.quantity} x ${Number(l.price).toFixed(2)}`,money(l.price*l.quantity)));}
    lines.push(center('-'.repeat(columns())));
    if(Number(order.discount))lines.push(pad('Discount','-'+money(order.discount)));
    if(Number(order.tax_total))lines.push(pad(profile?.prices_include_vat===false?`VAT ${profile.vat_rate}%`:`Incl. VAT ${profile?.vat_rate||''}%`,money(order.tax_total)));
    lines.push(bold(true),pad('TOTAL',money(order.total)),bold(false));
    for(const p of order.payments||[])lines.push(pad(`Paid ${p.method}`,money(p.amount)));
    if(Number(order.change))lines.push(pad('Change',money(order.change)));
    if(Number(order.due))lines.push(pad('Balance due',money(order.due)));
    lines.push('\n',align(1),center(profile?.receipt_footer||'Thank you for shopping with us.'),cut());
    await write(bytes(...lines));
  }
  async function testPrint(profile){await write(bytes(init(),align(1),bold(true),center(profile?.name||'Altab Tech'),bold(false),center('Printer test OK'),center(new Date().toLocaleString('en-GB')),cut()));}
  return {supported,connected,connect,disconnect,openDrawer,printReceipt,testPrint};
})();
