/* Illustrative mission replay only. No remote connection. */
(() => {
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
