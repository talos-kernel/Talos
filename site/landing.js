/* Local marketing interactions. Film is decorative; product captures are real. */
(() => {
 const reduced=matchMedia('(prefers-reduced-motion: reduce)');
 document.documentElement.classList.add('js');
 document.documentElement.dataset.theme='dark';
 const observer=new IntersectionObserver(entries=>entries.forEach(entry=>{if(entry.isIntersecting){entry.target.classList.add('in');observer.unobserve(entry.target);}}),{threshold:.08});
 document.querySelectorAll('.rv').forEach(el=>observer.observe(el));
 const video=document.getElementById('hero-film'), control=document.getElementById('film-control');
 let manualPause=false, inView=true, loaded=false;
 function loadFilm(){if(loaded)return;video.querySelector('source').src=video.querySelector('source').dataset.src;video.load();loaded=true;}
 function sync(){control.textContent=video.paused?'Play film':'Pause film';control.setAttribute('aria-label',video.paused?'Play background film':'Pause background film');}
 async function play(){loadFilm();try{await video.play();}catch{}sync();}
 function auto(){if(reduced.matches||navigator.connection?.saveData||manualPause||document.hidden||!inView){video.pause();sync();}else play();}
 control.addEventListener('click',()=>{if(video.paused){manualPause=false;play();}else{manualPause=true;video.pause();sync();}});
 video.addEventListener('play',sync);video.addEventListener('pause',sync);
 const visibility=new IntersectionObserver(entries=>{inView=entries[0].isIntersecting;auto();},{threshold:.05});visibility.observe(video);
 document.addEventListener('visibilitychange',auto);reduced.addEventListener('change',auto);
 const modes=[...document.querySelectorAll('[data-mode]')], product=document.getElementById('computer-image');
 modes.forEach(button=>button.addEventListener('click',()=>{const mode=button.dataset.mode;modes.forEach(b=>b.setAttribute('aria-pressed',String(b===button)));product.src='/media/computer-'+mode+'.webp';product.alt=mode==='headless'?'Actual headless Talos Computer with terminal output, jobs and files.':'Actual Talos Computer with a live desktop, jobs and files.';document.getElementById('computer-caption').textContent=mode==='headless'?'Actual product capture from a test workspace. Headless is the default; desktop is optional.':'Actual desktop capture from a test workspace. Take over, pause, or return control to Talos.';}));
 document.querySelectorAll('.cnt').forEach(el=>el.textContent=el.dataset.n+(el.dataset.suf||''));
 document.querySelectorAll('.copy').forEach(button=>button.addEventListener('click',async()=>{try{await navigator.clipboard.writeText('curl -fsSL https://talos-agent.ch/install.sh | bash');button.textContent='Copied';}catch{button.textContent='Select the command to copy';}setTimeout(()=>button.textContent='Copy command',2200);}));
})();
