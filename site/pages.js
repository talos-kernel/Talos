/* Public demonstrations only. No credential collection, agent requests or telemetry. */
(() => {
  'use strict';
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const announce = (id, text) => { const el = $(id); if (el) el.textContent = text; };

  $$('pre:has(code)').filter(pre => !pre.closest('.hero-terminal')).forEach(pre => {
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'copy-code'; button.textContent = 'Copy';
    button.setAttribute('aria-label', 'Copy code block');
    let timer;
    button.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText($('code', pre).textContent.trim());
        button.textContent = 'Copied';
      } catch { button.textContent = 'Select to copy'; }
      clearTimeout(timer); timer = setTimeout(() => { button.textContent = 'Copy'; }, 2200);
    });
    pre.append(button);
  });

  const docSearch = $('#doc-search');
  if (docSearch) {
    const sections = $$('[data-search-section]');
    const toc = $$('.toc a');
    const filter = () => {
      const query = docSearch.value.trim().toLocaleLowerCase('en');
      let found = 0;
      sections.forEach(section => {
        const match = !query || section.textContent.toLocaleLowerCase('en').includes(query);
        section.hidden = !match; found += Number(match);
        toc.filter(a => a.hash === '#' + section.id).forEach(a => { a.hidden = !match; });
      });
      announce('#doc-count', query ? `${found} of ${sections.length} sections` : `${sections.length} sections · full reference`);
      $('#doc-empty').hidden = found > 0;
    };
    docSearch.addEventListener('input', filter); filter();
    // A direct section link must remain readable even after a search.
    addEventListener('hashchange', () => {
      if (!docSearch.value) return;
      const target = sections.find(section => '#' + section.id === location.hash);
      if (target?.hidden) { docSearch.value = ''; filter(); target.scrollIntoView(); }
    });
  }

  const observerLinks = $$('.toc a');
  if ('IntersectionObserver' in window && observerLinks.length) {
    const observer = new IntersectionObserver(entries => {
      const entry = entries.find(e => e.isIntersecting);
      if (entry) observerLinks.forEach(a => a.classList.toggle('here', a.hash === '#' + entry.target.id));
    }, { rootMargin: '-15% 0px -65% 0px' });
    observerLinks.forEach(a => { const target = document.getElementById(a.hash.slice(1)); if (target) observer.observe(target); });
  }

  const configs = {"_doc": "Curated starting point for data/mcp-servers.json \u2014 copy it there and replace every placeholder with infrastructure you control. Commands must be absolute; an 'env' field costs the whole entry; the parser is fail-closed (see talos/mcpservers.py and docs/claude-worker.md).", "version": 1, "servers": [{"name": "chrome-devtools", "command": "/usr/local/bin/npx", "args": ["chrome-devtools-mcp@latest", "--headless=true"], "description": "Browser automation inside the job, started via npx"}, {"name": "filesystem", "command": "/usr/local/bin/npx", "args": ["@modelcontextprotocol/server-filesystem", "/var/lib/talos/claude-jobs"], "description": "File access rooted at the worker root \u2014 never wider"}]};
  const configButtons = $$('[data-config]');
  configButtons.forEach(button => button.addEventListener('click', () => {
    const server = configs.servers.find(item => item.name === button.dataset.config);
    if (!server) return;
    configButtons.forEach(b => b.setAttribute('aria-pressed', String(b === button)));
    announce('#registry-code', JSON.stringify({version: 1, servers: [server]}, null, 2));
    announce('#registry-caption', server.name === 'filesystem'
      ? 'Filesystem example. Replace the executable path and keep the allowed directory at or below the worker root. Review and pin the package version.'
      : 'Browser example. Replace the executable path and review the package before enabling it. Pin a reviewed package version for a reproducible deployment.');
  }));

  $$('[data-compare]').forEach(button => button.addEventListener('click', () => {
    $$('[data-compare]').forEach(b => b.setAttribute('aria-pressed', String(b === button)));
    let count = 0;
    $$('[data-compare-row]').forEach(row => {
      row.hidden = button.dataset.compare !== 'all' && row.dataset.category !== button.dataset.compare;
      count += Number(!row.hidden);
    });
    announce('#compare-count', `${count} ${count === 1 ? 'criterion' : 'criteria'} shown. Scroll horizontally on smaller screens.`);
  }));

  const caseSearch = $('#case-search');
  if (caseSearch) caseSearch.addEventListener('input', () => {
    let count = 0;
    $$('[data-case]').forEach(item => {
      item.hidden = !item.textContent.toLocaleLowerCase('en').includes(caseSearch.value.trim().toLocaleLowerCase('en'));
      count += Number(!item.hidden);
    });
    announce('#case-count', `${count} attack ${count === 1 ? 'surface' : 'surfaces'}`);
    $('#case-empty').hidden = count > 0;
  });

  $$('[data-channel]').forEach(button => button.addEventListener('click', () => {
    const telegram = button.dataset.channel === 'telegram';
    $$('[data-channel]').forEach(b => b.setAttribute('aria-pressed', String(b === button)));
    announce('#setup-connect-command', telegram ? 'python -m talos setup' : 'python -m talos setup terminal');
    announce('#setup-connect-copy', telegram
      ? 'Have your own Telegram bot token and operator account ID ready. Enter them in the local wizard, then configure the model connection there. Keep both out of this webpage.'
      : 'The terminal wizard records your local identity and guides you through the model connection. Telegram is optional.');
    announce('#setup-start-command', telegram ? 'python -m talos' : 'python -m talos chat');
    announce('#setup-start-title', telegram ? 'Start your Telegram agent.' : 'Open your terminal session.');
    announce('#setup-start-copy', telegram
      ? 'Start Talos in the foreground, then open your bot in Telegram with the account you allowed. The process stays running while it polls for messages.'
      : 'Start a conversation in this terminal. The session header shows the active model and whether approvals are available.');
    announce('#setup-channel-note', telegram
      ? 'Send /help in the private bot conversation. An account outside your allowlist cannot command the agent.'
      : 'Use /help for session commands. Type exit when you are finished.');
  }));

  const events = [{"time": "00:00", "type": "task.received", "title": "A coding task arrives", "tone": "neutral", "decision": "Received", "summary": "The operator asks for a scoped code change and a testable result. The conversation becomes a task with a recorded identity.", "detail": {"event": "task.received", "channel": "operator chat", "request": "Make the scoped change and show the check."}}, {"time": "00:01", "type": "reason.started", "title": "The model considers the task", "tone": "neutral", "decision": "Reasoning", "summary": "The model receives the task and the available tools. Reasoning can propose an action; it cannot grant authority.", "detail": {"event": "reason.started", "authority": "unchanged"}}, {"time": "00:03", "type": "exec.intent", "title": "A protected path is requested", "tone": "denied", "decision": "Denied", "summary": "A proposed raw shell action targets a protected worker path. The policy rejects the request before execution.", "detail": {"event": "exec.intent", "tool": "shell", "verdict": "DENY", "reason": "protected worker boundary"}}, {"time": "00:03", "type": "exec.result", "title": "The denied action does not run", "tone": "denied", "decision": "No execution", "summary": "No grant is issued for the refused action. The agent sees the denial and can propose a permitted route.", "detail": {"event": "exec.result", "executed": false, "grant_issued": false}}, {"time": "00:05", "type": "reason.started", "title": "The agent chooses the worker route", "tone": "neutral", "decision": "Replanning", "summary": "The next proposal uses the explicit coding worker tool. It describes a job instead of trying to reach protected worker infrastructure.", "detail": {"event": "reason.started", "next_tool": "delegate_code"}}, {"time": "00:08", "type": "exec.intent", "title": "The new request passes the gate", "tone": "allowed", "decision": "Allowed", "summary": "The declared task, principal and worker configuration satisfy the policy in this example. The original denial has not been overridden.", "detail": {"event": "exec.intent", "tool": "delegate_code", "verdict": "ALLOW"}}, {"time": "00:08", "type": "grant.issued", "title": "Authority is bound to this call", "tone": "allowed", "decision": "One exact call", "summary": "The executor receives a short-lived, single-use grant for the exact arguments that passed the kernel. It is not permission for arbitrary later commands.", "detail": {"event": "grant.issued", "bound_to": "exact arguments", "single_use": true, "lifetime_seconds": 30}}, {"time": "00:09", "type": "exec.result", "title": "The confined worker accepts the job", "tone": "allowed", "decision": "Accepted", "summary": "The worker accepts the request with a workspace and deadline. Acceptance is the start of the job, not proof of completion.", "detail": {"event": "exec.result", "job_state": "accepted", "confinement": "workspace and deadline"}}, {"time": "00:10", "type": "reply.sent", "title": "The operator gets a clear receipt", "tone": "neutral", "decision": "Acknowledged", "summary": "The agent reports that the job has been accepted. The reply distinguishes queued work from a verified result.", "detail": {"event": "reply.sent", "status": "accepted", "completion_claimed": false}}, {"time": "00:11", "type": "turn.done", "title": "The conversation turn finishes", "tone": "neutral", "decision": "Worker continues", "summary": "The interactive turn can end while a delegated job continues. The job retains its own state and result.", "detail": {"event": "turn.done", "worker_state": "running"}}, {"time": "03:32", "type": "job.completed", "title": "The worker reports completion", "tone": "allowed", "decision": "Reported done", "summary": "The worker finishes and returns its output. A completed worker turn is still subject to the relevant artifact or test inspection.", "detail": {"event": "job.completed", "job_state": "done", "result_kind": "worker report"}}, {"time": "03:37", "type": "notify.pushed", "title": "The result reaches the conversation", "tone": "allowed", "decision": "Ready to inspect", "summary": "The completion receipt is delivered to the originating conversation. Inspect the actual files and checks before treating the task as verified.", "detail": {"event": "notify.pushed", "destination": "originating conversation", "next_step": "inspect artifacts and checks"}}];
  const replay = $('#replayBtn');
  if (replay) {
    const buttons = $$('[data-event]');
    const pause = $('#pauseBtn'), reset = $('#resetBtn');
    let current = 0, timer = null, playing = false;
    const stop = () => { clearTimeout(timer); timer = null; playing = false; pause.disabled = true; replay.disabled = false; };
    function select(index, scroll = false) {
      current = index; const event = events[index];
      buttons.forEach((button, i) => button.setAttribute('aria-pressed', String(i === index)));
      announce('#event-type', event.type.toUpperCase()); announce('#event-title', event.title);
      announce('#event-summary', event.summary); announce('#event-decision', event.decision);
      announce('#event-detail', JSON.stringify(event.detail, null, 2));
      $('#replay-progress').style.transform = `scaleX(${(index + 1) / events.length})`;
      if (scroll) {
        const list = $('.event-list'), button = buttons[index];
        list.scrollTo({top: button.offsetTop - list.offsetTop - list.clientHeight / 2 + button.clientHeight / 2, behavior: 'instant'});
      }
    }
    function tick() {
      if (!playing) return;
      select(current, true);
      announce('#replay-status', `Playing example · ${current + 1} / ${events.length}`);
      if (current === events.length - 1) {
        stop(); replay.textContent = 'Replay'; announce('#replay-status', `Example finished · ${events.length} events`); return;
      }
      timer = setTimeout(() => { current++; tick(); }, 950);
    }
    replay.addEventListener('click', () => {
      const resume = replay.textContent === 'Resume'; stop();
      if (reduced.matches) {
        select(events.length - 1, true); replay.textContent = 'Replay';
        announce('#replay-status', 'Example complete · reduced motion'); return;
      }
      if (!resume || current >= events.length - 1) current = 0;
      playing = true; pause.disabled = false; replay.disabled = true; replay.textContent = 'Playing'; tick();
    });
    pause.addEventListener('click', () => { stop(); replay.textContent = 'Resume'; announce('#replay-status', `Paused · ${current + 1} / ${events.length}`); });
    reset.addEventListener('click', () => { stop(); select(0, true); replay.textContent = 'Replay'; announce('#replay-status', `Example ready · ${events.length} events`); });
    buttons.forEach((button, index) => button.addEventListener('click', () => {
      stop(); select(index); replay.textContent = 'Replay'; announce('#replay-status', `Inspecting example · ${index + 1} / ${events.length}`);
    }));
    document.addEventListener('visibilitychange', () => {
      if (document.hidden && playing) { stop(); replay.textContent = 'Resume'; announce('#replay-status', 'Paused while this page is hidden'); }
    });
    reduced.addEventListener('change', () => { if (reduced.matches && playing) {stop(); select(events.length - 1); replay.textContent = 'Replay'; announce('#replay-status', 'Example complete · reduced motion');} });
    select(0);
  }

  const refresh = $('#statusRefresh');
  if (refresh) {
    let controller = null;
    async function loadStatus() {
      if (controller) controller.abort();
      controller = new AbortController(); const current = controller;
      const timeout = setTimeout(() => current.abort(), 8000);
      refresh.disabled = true; announce('#statusSrc', 'Loading published release data…');
      try {
        const response = await fetch('/status.json', {cache: 'no-store', signal: current.signal});
        if (!response.ok) throw new Error('status unavailable');
        const data = await response.json();
        const numeric = ['tests', 'redteam_faelle', 'kernel_zeilen', 'werkzeuge'];
        if (!data || !/^\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?$/.test(data.version)
          || numeric.some(key => !Number.isSafeInteger(data[key]) || data[key] < 0 || data[key] > 10000000)
          || typeof data.sha !== 'string' || !/^[a-f0-9]{7,40}$/.test(data.sha)
          || typeof data.gebaut_am !== 'string' || !Number.isFinite(Date.parse(data.gebaut_am))) throw new Error('invalid status data');
        if (controller !== current) return;
        $$('[data-f]').forEach(el => { el.textContent = String(data[el.dataset.f]); });
        announce('#statusSrc', `Source: /status.json · commit ${data.sha.slice(0, 10)} · ${data.kernel_zeilen} kernel lines · built ${new Date(data.gebaut_am).toISOString().slice(0, 10)} · fetched ${new Date().toLocaleTimeString('en-GB', {hour: '2-digit', minute: '2-digit'})}`);
      } catch {
        if (controller !== current) return;
        $$('[data-f]').forEach(el => { el.textContent = '—'; });
        announce('#statusSrc', 'Published release data is unavailable. Try Refresh or inspect status.json directly.');
      } finally { clearTimeout(timeout); if (controller === current) { refresh.disabled = false; controller = null; } }
    }
    refresh.addEventListener('click', loadStatus); loadStatus();
  }

  // Reveal only short editorial sections. Long reference sections stay readable.
  if (!reduced.matches && 'IntersectionObserver' in window) {
    const sections = $$('.agent-profile, .decision-grid > div, .boundary-note > div, .evidence-strip > div');
    const observer = new IntersectionObserver(entries => entries.forEach(entry => {
      if (entry.isIntersecting) { entry.target.classList.add('in'); observer.unobserve(entry.target); }
    }), {threshold: .08});
    sections.forEach(section => { section.classList.add('section-reveal'); observer.observe(section); });
    document.documentElement.classList.add('motion-ready');
  }
})();
