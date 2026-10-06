/* Marble Hall interactions. Motion never controls access to content. */
(() => {
  const nav=document.querySelector('.marble-nav');
  const menu=document.querySelector('.mobile-menu');
  const closeMenu=()=>{nav?.removeAttribute('data-open');menu?.setAttribute('aria-expanded','false');if(menu)menu.textContent='Menu';};
  menu?.addEventListener('click',()=>{const open=!nav.hasAttribute('data-open');nav.toggleAttribute('data-open',open);menu.setAttribute('aria-expanded',String(open));menu.textContent=open?'Close':'Menu';});
  nav?.querySelectorAll('a').forEach(a=>a.addEventListener('click',closeMenu));
  document.addEventListener('keydown',e=>{if(e.key==='Escape'){const wasOpen=nav?.hasAttribute('data-open');closeMenu();document.querySelectorAll('.explore[open]').forEach(d=>d.open=false);if(wasOpen)menu.focus();}});
  document.addEventListener('click',e=>{document.querySelectorAll('.explore[open]').forEach(d=>{if(!d.contains(e.target))d.open=false;});});
  const media=matchMedia('(prefers-reduced-motion: reduce)');
  let paused=media.matches;
  const pause=document.querySelector('.motion-toggle');
  const setPause=()=>{document.body.classList.toggle('motion-paused',paused);if(pause){pause.setAttribute('aria-pressed',String(paused));pause.textContent=paused?'Resume motion':'Pause motion';}};
  setPause();pause?.addEventListener('click',()=>{paused=!paused;setPause();});media.addEventListener('change',()=>{paused=media.matches;setPause();});
  const art=document.querySelector('.hall-art');
  art?.addEventListener('pointermove',e=>{if(paused||media.matches)return;const r=art.getBoundingClientRect();art.style.setProperty('--px',((e.clientX-r.left)/r.width*2-1).toFixed(3));art.style.setProperty('--py',((e.clientY-r.top)/r.height*2-1).toFixed(3));});
  art?.addEventListener('pointerleave',()=>{art.style.setProperty('--px','0');art.style.setProperty('--py','0');});
  const footer=document.querySelector('footer');
  if(footer){const word=document.createElement('div');word.className='footer-wordmark';word.setAttribute('aria-hidden','true');word.textContent='TALOS';footer.append(word);}
  // Deep tables remain scrollable inside their own region, never the entire page.
  document.querySelectorAll('.marble-document table').forEach(table=>{const wrap=document.createElement('div');wrap.className='table-wrap';table.before(wrap);wrap.append(table);});
})();
