// Apply the saved identity before the application loads (also used on login).
(() => {
  const data=document.getElementById('store-branding');
  if(!data)return;
  const p=JSON.parse(data.textContent), root=document.documentElement;
  root.style.setProperty('--green',p.primary_color);
  root.style.setProperty('--green-dark',p.primary_color);
  root.style.setProperty('--accent',p.accent_color);
  const [r,g,b]=p.primary_color.match(/[a-f\d]{2}/gi).map(v=>parseInt(v,16));
  root.style.setProperty('--on-primary',r*.299+g*.587+b*.114>165?'#17211c':'#ffffff');
  const apply=()=>root.dataset.theme=p.theme==='system'?(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'):p.theme;
  apply();
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change',()=>{if(!window.STOREFRONT&&!document.getElementById('app'))apply();});
})();
