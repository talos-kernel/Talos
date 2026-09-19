"""Semantic browser actions inside the guest, attached to its persistent Chromium.

Only the guest's loopback CDP endpoint is used. Host credentials, sockets and browser
profiles are never imported. Receipts report observations, not invented completion.
"""
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit, urlunsplit

ENDPOINT = "http://127.0.0.1:9222"

# The state of one observed element, as a single string. Deliberately excludes
# geometry and surrounding text: a ticker or lazy-loaded sidebar may change the
# page freely, but the element a decision was made about may not change unseen.
# Secret values are masked here too, so a guard never carries a password.
GUARD_JS = r"""e => {
  const secret = e.type === 'password' || /cc-|one-time-code/.test(e.autocomplete || '');
  return [e.tagName, e.type || '', e.name || '', e.id || '',
    e.disabled ? '1' : '0', e.checked ? '1' : '0', e.required ? '1' : '0',
    secret ? '[private]' : String(e.value || '').slice(0, 200)].join('\u001f');
}"""

# The name a screen reader would announce, in the order the accessibility tree
# resolves it. innerText alone mislabels icon buttons and aria-labelledby layouts,
# and a wrong label is what makes a model pick the wrong field.
NAME_JS = r"""(e, seen) => {
  seen = seen || new Set();
  if (!e || seen.has(e)) return '';
  seen.add(e);
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const by = e.getAttribute && e.getAttribute('aria-labelledby');
  if (by) {
    const parts = by.split(/\s+/).map(id => {
      const t = e.ownerDocument.getElementById(id);
      return t ? clean(accName(t, seen) || t.innerText) : '';
    }).filter(Boolean);
    if (parts.length) return parts.join(' ');
  }
  const aria = clean(e.getAttribute && e.getAttribute('aria-label'));
  if (aria) return aria;
  const label = e.labels && e.labels[0];
  if (label) { const s = clean(label.innerText); if (s) return s; }
  const alt = clean(e.getAttribute && e.getAttribute('alt'));
  if (alt) return alt;
  const own = clean(Array.from(e.childNodes).map(n => {
    if (n.nodeType === 3) return n.nodeValue;
    if (n.nodeType === 1 && !n.hasAttribute('aria-hidden')) return n.innerText || n.getAttribute('alt') || '';
    return '';
  }).join(' '));
  if (own) return own;
  const title = clean(e.getAttribute && e.getAttribute('title'));
  if (title) return title;
  const placeholder = clean(e.placeholder);
  if (placeholder) return placeholder;
  return clean(e.name);
}"""

# getClientRects() only proves layout, not reachability: a field under a cookie
# banner measures fine and then swallows eight seconds of click timeout. Geometry
# is resolved fresh here, never remembered, and names the blocker instead of the
# symptom. Outside the viewport the answer is unknown, not blocked — scrolling decides.
BLOCKED_JS = r"""e => {
  const r = e.getClientRects()[0];
  if (!r) return null;
  const view = e.ownerDocument.defaultView || window;
  if (r.bottom < 0 || r.right < 0 || r.top > view.innerHeight || r.left > view.innerWidth) return null;
  const x = Math.min(Math.max(r.left + r.width / 2, 1), view.innerWidth - 1);
  const y = Math.min(Math.max(r.top + r.height / 2, 1), view.innerHeight - 1);
  const hit = e.ownerDocument.elementFromPoint(x, y);
  if (!hit || e.contains(hit) || hit.contains(e)) return null;
  return (accName(hit) || hit.tagName.toLowerCase()).slice(0, 80);
}"""

FIELDS_JS = r"""els => {
  const accName = """ + NAME_JS + r""";
  const guardSource = """ + GUARD_JS + r""";
  const blockedBy = """ + BLOCKED_JS + r""";
  return els.filter(e => e.getClientRects().length).slice(0, 40).map((e, i) => {
    if (!e.dataset.talosRef) e.dataset.talosRef = 't' + Date.now() + '-' + i;
    const secret = e.type === 'password' || /cc-|one-time-code/.test(e.autocomplete || '');
    return {selector:'[data-talos-ref="' + e.dataset.talosRef + '"]', tag:e.tagName.toLowerCase(),
      type:e.type || '', label:accName(e).slice(0, 100),
      value:secret ? '[private]' : String(e.value || '').slice(0, 200), required:!!e.required,
      disabled:!!e.disabled, checked:!!e.checked, valid:e.validity ? e.validity.valid : null,
      validation:(e.validationMessage || '').slice(0, 180),
      blocked_by:blockedBy(e), guard_source:guardSource(e),
      options:e.tagName === 'SELECT' ? Array.from(e.options).slice(0, 20).map(o => ({value:o.value, label:o.text, disabled:o.disabled})) : undefined};
  });
}"""


def guard(source):
    """Short digest of one element's observed state, safe to carry in a receipt."""
    return hashlib.sha256(source.encode("utf-8", "replace")).hexdigest()[:32]


def _verbinden(pw):
    """Verbindet sich mit dem laufenden Browser — oder sagt genau, was fehlt.

    ⚠️ Ohne diese Stelle kommt ein toter Browser als nackter `ECONNREFUSED
    127.0.0.1:9222` an. Gemessen am 12.09.2026: ein Agent brauchte daraufhin ueber
    zwanzig Minuten, um von dieser Meldung bis zur Ursache zu kommen — er musste
    selbst darauf verfallen, dass hinter dem Port ein Dienst steht, wie der heisst
    und wie man ihn startet. Die Aufgabe selbst dauerte danach unter einer Minute.

    Das ist derselbe Unterschied, den `remedy.py` fuer den Agenten macht: ein
    fehlender Dienst ist ein MANGEL, kein Verdikt. Ein Mangel gehoert benannt, samt
    dem einen Kommando, das ihn behebt. Wer stattdessen eine Portnummer zurueckgibt,
    laesst den Agenten raten — und Raten kostet genau die Zeit, die hier verloren ging.
    """
    from playwright.sync_api import Error as PlaywrightError

    try:
        return pw.chromium.connect_over_cdp(ENDPOINT, timeout=10000)
    except PlaywrightError as fehler:
        raise RuntimeError(
            f"the browser inside the computer is not reachable on {ENDPOINT}. "
            "It runs as `talos-browser.service` in the guest; start it there with "
            "`systemctl start talos-browser` and check `systemctl status "
            "talos-browser` if it refuses. Nothing was clicked or typed."
        ) from fehler


def tab_id(context, page):
    session = context.new_cdp_session(page)
    try:
        return session.send("Target.getTargetInfo")["targetInfo"]["targetId"]
    finally:
        session.detach()


def select_page(context, args):
    pages = context.pages
    if "tab" in args:
        for page in pages:
            if tab_id(context, page) == args["tab"]:
                return page
        raise ValueError("browser tab no longer exists; inspect available tabs before continuing")
    index = args.get("page", len(pages) - 1)
    if index >= len(pages):
        raise ValueError("browser tab not found; inspect the available tabs")
    return pages[index] if pages else context.new_page()


def public_url(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def snapshot(page, scope):
    # Hidden fields and password values never enter the agent transcript. Stable
    # references let a model use observed targets instead of guessing coordinates.
    fields = scope.locator("input:not([type=hidden]),select,textarea,button,a[href],[role=button]").evaluate_all(FIELDS_JS)
    # The guard is hashed on this side: the raw state string never needs to travel.
    for field in fields:
        field["guard"] = guard(field.pop("guard_source", ""))
    frames = page.locator('iframe').evaluate_all("""els => els.slice(0,10).map((e,i) => {
      if(!e.dataset.talosFrame)e.dataset.talosFrame='frame-'+i;
      return {selector:'[data-talos-frame="'+e.dataset.talosFrame+'"]',url:e.src,name:e.name,title:e.title};
    })""")
    for frame in frames:
        frame['url'] = public_url(frame['url'])
    challenge = page.locator('iframe[src*="bframe"],iframe[title*="challenge" i],iframe[title*="security challenge" i]')
    interactive = any(challenge.nth(i).is_visible() for i in range(min(challenge.count(), 8)))
    body = scope.locator("body").inner_text(timeout=3000)
    # A passive v3 badge is not a blocked task. Only a visible challenge asks for help.
    blocked_text = ("just a moment" in page.title().lower() or "/cdn-cgi/" in page.url) and any(
        s in body.lower() for s in ("verify you are human", "verifying you are human", "confirm you are human"))
    alerts = scope.locator('[role="alert"],.toast-message,.toast-title,.alert').all_inner_texts()
    return {"url": public_url(page.url), "title": page.title(), "alerts": [a[:800] for a in alerts[:6]],
            "text": body[:2000], "fields": fields, "frames": frames,
            "guards": "pass a field's guard as `expect` to have the next action refuse a changed element",
            "challenge": {"interactive": interactive or blocked_text,
                          "instruction": "Use the operator takeover if confirmation is required; inspect again before continuing." if interactive or blocked_text else ""}}


def run(args):
    from playwright.sync_api import sync_playwright
    from contract import validate
    from guest import project_path, safe_file
    validate(args)
    root = project_path(args["project"])
    with sync_playwright() as pw:
        browser = _verbinden(pw)
        context = browser.contexts[0]
        # Chromium target order can change between CDP connections. Prefer the
        # observed target ID; a missing ID never falls back to a different tab.
        page = select_page(context, args)
        selected_tab = tab_id(context, page)
        page.set_default_timeout(8000)
        page.bring_to_front()
        errors, responses, submissions = [], [], []
        page.on("pageerror", lambda e: errors.append(str(e)[:160]))
        page.on("response", lambda r: responses.append({"status": r.status, "url": public_url(r.url)}) if r.request.resource_type in {"document", "xhr", "fetch"} else None)
        def finished(request):
            if request.method in {"GET", "HEAD", "OPTIONS"} or request.resource_type not in {"document", "xhr", "fetch"}:
                return
            response = request.response()
            if response is None:
                return
            item = {"status":response.status, "url":public_url(response.url)}
            # Only ordinary receipt fields, never response tokens or cookies.
            try:
                if "application/json" in response.headers.get("content-type", ""):
                    value = response.json()
                    if isinstance(value, dict):
                        item["receipt"] = {k:v for k,v in value.items() if k in
                            {"status", "code", "message", "success", "error", "detail", "reference", "confirmation_id"}
                            and isinstance(v, (str, int, float, bool, type(None))) and len(str(v)) <= 1000}
            except Exception:
                pass
            submissions.append(item)
        page.on("requestfinished", finished)
        action = args["action"]
        scope = page.frame_locator(args["frame"]) if args.get("frame") else page
        before_url = public_url(page.url)
        submission_origins = {urlsplit(page.url).netloc}
        effect, observed = False, None
        if action == "navigate":
            page.goto(args["url"], wait_until="domcontentloaded", timeout=25000)
            effect = True
        elif action != "inspect":
            target = scope.locator(args["selector"])
            # Strict uniqueness avoids silently selecting the first of two matching
            # Submit buttons, particularly inside nested or repeated forms.
            if target.count() != 1:
                raise ValueError("target must match exactly one observed element; inspect again")
            # Code checks freshness, not the model: an element that changed since it
            # was inspected is a different decision, so the action does not happen.
            if "expect" in args and guard(target.evaluate(GUARD_JS)) != args["expect"]:
                return {"state":"needs_review", "action_performed":False, "tab":selected_tab,
                        "reason":"the observed element changed since it was inspected",
                        "page":snapshot(page, scope)}
            if action in {"fill", "type"}:
                kind = target.get_attribute("type")
                if kind in {"password", "hidden"} or target.get_attribute("autocomplete") in {"one-time-code", "cc-number", "cc-csc"}:
                    raise ValueError("private field requires operator takeover")
                if action == "fill":
                    target.fill(args["value"])
                else:
                    target.click(); target.press("ControlOrMeta+A")
                    target.press_sequentially(args["value"], delay=20)
                target.press("Tab")
                observed = target.input_value()
            elif action == "select":
                target.select_option(value=args["value"])
                observed = target.input_value()
            elif action == "check":
                target.set_checked(args.get("checked", True)); observed = target.is_checked()
            elif action == "press":
                target.press(args["value"])
            elif action == "wait":
                target.wait_for(state="visible")
            elif action == "upload":
                target.set_input_files(str(safe_file(root, args["path"])))
                observed = "project file selected"
            else:
                if action == "submit":
                    form_action = target.evaluate("e => { const f=e.form || e.closest('form'); return f ? f.action : ''; }")
                    if form_action:
                        submission_origins.add(urlsplit(form_action).netloc)
                    valid = target.evaluate("e => { const f=e.form || e.closest('form'); return f ? f.checkValidity() : true; }")
                    if not valid:
                        return {"state":"needs_review", "action_performed":False, "tab":selected_tab,
                                "reason":"required fields are invalid", "page":snapshot(page, scope)}
                    if snapshot(page, scope)["challenge"]["interactive"]:
                        return {"state":"needs_review", "action_performed":False, "tab":selected_tab,
                                "reason":"operator challenge confirmation required", "page":snapshot(page, scope)}
                target.click()
            effect = action != "wait"
        if action == "submit" and effect:
            # Mail-backed forms often return after the click's navigation timeout
            # window. Keep their short-lived confirmation in the same receipt.
            deadline = time.monotonic() + 10
            # CAPTCHA and analytics POSTs can finish before the actual form.
            while not any(urlsplit(r["url"]).netloc in submission_origins for r in submissions) and time.monotonic() < deadline:
                page.wait_for_timeout(100)
        # Allow ordinary async validation/navigation to settle, without waiting for
        # global network-idle on sites that poll forever.
        page.wait_for_timeout(350)
        observed_page = snapshot(page, scope)
        receipt = {"state":"needs_review", "action":action, "action_performed":effect, "tab":selected_tab,
                   "observed_value":observed, "previous_url":before_url,
                   "submissions":submissions[-6:], "responses":responses[-12:], "page_errors":errors[:5],
                   "page":observed_page,
                   "tabs":[{"page":i,"tab":tab_id(context,p),"url":public_url(p.url)} for i,p in enumerate(context.pages[:30])],
                   "verification":"browser observations only; check the real confirmation and server response"}
        (root / "browser-last.json").write_text(json.dumps(receipt, ensure_ascii=False))
        page.screenshot(path=str(root / "browser-last.png"), full_page=False)
        return receipt


if __name__ == "__main__":
    try:
        print(json.dumps(run(json.loads(sys.stdin.buffer.readline(40001))), ensure_ascii=False))
    except Exception as error:
        print(json.dumps({"state":"failed", "action_performed":None,
                          "reason":str(error)[:300], "verification":"inspect before retrying; no completion claimed"}))
