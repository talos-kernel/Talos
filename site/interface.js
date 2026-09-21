/* Public, local-only interface interactions. No agent connection or telemetry. */
(() => {
  /* The Watcher lets the pointer part the dust. The shared surface gets a quieter
     version so the interaction belongs to Talos as a whole, not one hidden page. */
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const finePointer = matchMedia('(pointer: fine)').matches;
  if (!reduced && finePointer) {
    const canvas = document.createElement('canvas');
    canvas.id = 'talos-dust';
    canvas.setAttribute('aria-hidden', 'true');
    document.body.prepend(canvas);
    const ctx = canvas.getContext('2d');
    const particles = Array.from({length: 1400}, (_, index) => ({
      x: Math.random(), y: Math.random(),
      size: index % 11 === 0 ? 2 : 1,
      phase: Math.random() * Math.PI * 2,
      speed: .00008 + Math.random() * .00012,
      hot: index % 13 === 0,
    }));
    let width = 0, height = 0, dpr = 1;
    let pointer = {x: -9999, y: -9999};

    function resize() {
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      width = window.innerWidth; height = window.innerHeight;
      canvas.width = width * dpr; canvas.height = height * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    function frame(now) {
      ctx.clearRect(0, 0, width, height);
      if (pointer.x > -1000) {
        const aura = ctx.createRadialGradient(pointer.x, pointer.y, 0, pointer.x, pointer.y, 150);
        aura.addColorStop(0, 'oklch(0.78 0.115 74 / .075)');
        aura.addColorStop(1, 'oklch(0.78 0.115 74 / 0)');
        ctx.fillStyle = aura;
        ctx.fillRect(pointer.x - 150, pointer.y - 150, 300, 300);
      }
      for (const particle of particles) {
        const drift = now * particle.speed + particle.phase;
        let x = particle.x * width + Math.sin(drift) * 10;
        let y = particle.y * height + Math.cos(drift * .8) * 8;
        const dx = x - pointer.x, dy = y - pointer.y;
        const distance = Math.hypot(dx, dy);
        if (distance < 150) {
          const force = (150 - distance) / 150 * 28;
          const safe = distance || 1;
          x += dx / safe * force; y += dy / safe * force;
        }
        ctx.fillStyle = particle.hot ? 'oklch(0.78 0.115 74 / .78)' : 'oklch(0.66 0.095 68 / .48)';
        ctx.fillRect(x, y, particle.size, particle.size);
      }
      requestAnimationFrame(frame);
    }

    resize();
    window.addEventListener('resize', resize, {passive:true});
    window.addEventListener('pointermove', event => {
      pointer.x = event.clientX; pointer.y = event.clientY;
    }, {passive:true});
    window.addEventListener('pointerleave', () => { pointer = {x:-9999, y:-9999}; }, {passive:true});
    requestAnimationFrame(frame);
  }

  const nav = document.querySelector('nav');
  if (nav) {
    const wrap = nav.querySelector('.wrap');
    const links = [...nav.querySelectorAll('a.lnk')];
    if (wrap && links.length) {
      const group = document.createElement('div');
      group.className = 'nav-links'; group.id = 'site-navigation';
      links.forEach(link => group.append(link));
      const toggle = document.createElement('button');
      toggle.type = 'button'; toggle.className = 'menu-toggle'; toggle.textContent = 'Gate +';
      toggle.setAttribute('aria-expanded', 'false'); toggle.setAttribute('aria-controls', group.id);
      const close = () => { nav.removeAttribute('data-open'); toggle.setAttribute('aria-expanded','false'); toggle.textContent='Gate +'; };
      toggle.addEventListener('click', () => {
        if (nav.hasAttribute('data-open')) close();
        else { nav.setAttribute('data-open',''); toggle.setAttribute('aria-expanded','true'); toggle.textContent='Close Gate'; }
      });
      nav.addEventListener('keydown', event => { if (event.key==='Escape') {close();toggle.focus();} });
      links.forEach(link => link.addEventListener('click', close));
      wrap.append(toggle,group); nav.setAttribute('data-menu',''); nav.setAttribute('aria-label','Main navigation');
    }
    const target = document.querySelector('header, main');
    if (target) {
      if (!target.id) target.id='main-content';
      target.tabIndex=-1;
      const skip=document.createElement('a');skip.className='skip-link';skip.href='#'+target.id;skip.textContent='Skip to content';
      document.body.prepend(skip);
    }
  }
  const preview=document.getElementById('mission-preview');
  if (!preview) return;
  const scenarios={
    build:{request:'Mend the failed test. Show me what was changed.',state:'DEED RECORDED',meta:'Illustration · 34s · 4 tool calls',
      steps:[['I','Read the failure and the code around it.'],['II','Give the bounded task to the sworn worker.'],['III','Read back the patch and prove the result.'],['IV','Report what was proved and what remains.']],
      result:'The patch stands ready for inspection. The worker record is followed by a separate proof.'},
    approval:{request:'Run this maintenance command upon the server.',state:'THE WRIT AWAITS',meta:'Illustration · remote effect · awaiting your word',
      steps:[['I','Name the host held by the operator.'],['II','Examine the exact host and command.'],['III','Set the deed before the one who may grant it.']],
      result:'Nothing remote stirs until the operator grants it. A spoken reply cannot grant its own authority.'},
    failure:{request:'Forge the change with the coding worker.',state:'WORKER ABSENT',meta:'Illustration · missing provision · no deed begun',
      steps:[['I','Choose the worker sworn for this task.'],['II','The worker names the missing provision.'],['III','Name the next provision or another sworn path.']],
      result:'A plain failure with a named next step. No invented victory and no unguarded fallback.'}
  };
  const request=preview.querySelector('[data-request]'),state=preview.querySelector('[data-state]');
  const meta=preview.querySelector('[data-meta]'),trail=preview.querySelector('[data-trail]'),result=preview.querySelector('[data-result]');
  const play=preview.querySelector('[data-play]');
  const controls=[...document.querySelectorAll('[data-scenario]')];
  let current='build',timer=null;
  const stop=()=>{clearTimeout(timer);timer=null;play.disabled=false;play.textContent='Read the deed again';};
  function render(count,playing=false){
    const item=scenarios[current];request.textContent=item.request;
    state.textContent=playing?'WORKING':item.state;
    meta.textContent=playing?'Illustrative sequence · not a live agent':item.meta;
    trail.replaceChildren(...item.steps.slice(0,count).map(([icon,text])=>{
      const row=document.createElement('li'),glyph=document.createElement('span'),label=document.createElement('span');
      glyph.textContent=icon;glyph.setAttribute('aria-hidden','true');label.textContent=text;row.append(glyph,label);return row;
    }));
    result.textContent=playing?'The record is being unfurled…':item.result;
  }
  controls.forEach(button=>button.addEventListener('click',()=>{
    stop();current=button.dataset.scenario;
    controls.forEach(control=>control.setAttribute('aria-pressed',String(control===button)));
    render(scenarios[current].steps.length);
  }));
  play.addEventListener('click',()=>{
    stop();
    if(matchMedia('(prefers-reduced-motion: reduce)').matches){render(scenarios[current].steps.length);return;}
    play.disabled=true;play.textContent='Unfurling the record…';let count=0;
    function advance(){count++;const finished=count>=scenarios[current].steps.length;render(count,!finished);
      if(finished)stop();else timer=setTimeout(advance,850);}
    advance();
  });
  document.addEventListener('visibilitychange',()=>{if(document.hidden){stop();render(scenarios[current].steps.length);}});
  render(scenarios[current].steps.length);
})();
