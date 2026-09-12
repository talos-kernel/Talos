# Talos Computer (experimental)

An optional, persistent Linux computer for one operator. Headless by default, with an opt-in desktop. The model proposes an action;
the normal Talos kernel asks for that exact action before the backend executes it
inside an ARM64 KVM virtual machine.

## What is implemented

- Terminal, scripts and persistent `/home/desk/Projects/<project>` directories. A semantic browser handles forms headlessly; add `--desktop` for a graphical workspace and live takeover.
- Authenticated workbench with current screenshots, live desktop takeover, pause,
  stop, project files, job receipts and private routine templates.
- Two manifest tools: `computer_status` (read) and `computer_run` (exec).
- Durable operation keys. Repeating identical arguments with the same key retrieves
  the original receipt; a different request under that key is rejected.
- Explicit `verified`, `needs_review`, `failed` and `interrupted` outcomes.
  A zero exit code alone is `needs_review`. Verification means the requested
  relative files matched their expected SHA-256 hashes, not that all business
  requirements or visual quality were independently proved.
- Interrupted jobs stay interrupted after restart or takeover. They never resume
  automatically. A new attempt needs inspection, a new operation key and its own
  normal approval.

This is an experimental source feature. The ordinary installer does not provision
a desktop automatically. The initial backend supports **ARM64 Debian Linux with
KVM**; macOS, Windows and x86 desktop backends are not implemented.

## Runtime access and existing user services

The API runtime directory grants the configured agent UID a named-user ACL. The
control socket inherits read/write access; the raw VNC socket remains mode 0600
with that inherited ACL masked out. Peer credentials and the owner binding are
still checked by the API. This avoids relying on supplementary groups cached by
an already-running systemd user manager.

If an older deployment reports `computer_status: Permission denied`, inspect the
actual agent process credentials in `/proc/<pid>/status`, not just `id` in a fresh
SSH session. Configure the equivalent named-user runtime ACL for that exact UID;
do not make the sockets world-writable or restart the entire user manager.

## Provisioning

Requirements: ARM64 Linux, `/dev/kvm`, systemd, root for provisioning, Internet for
Debian packages, 2 GB guest RAM for headless or 4 GB for desktop, two guest CPUs and a 40 GB virtual disk. Review
`deploy/computer-setup.py`, the four systemd units and cloud-init template first.

Use your existing service account and your channel-qualified operator identity:

```bash
python3 deploy/computer-setup.py --check \
  --agent-user agent --owner telegram:123456789 \
  --origin https://your-private-host.example:8443

sudo python3 deploy/computer-setup.py \
  --agent-user agent --owner telegram:123456789 \
  --origin https://your-private-host.example:8443
```

Add `--desktop` to either command to include XFCE, Chromium and live takeover.
Headless installs omit graphical packages, VNC forwarding and desktop actions.
The web workbench still shows jobs, terminal output, project files and routines.
An existing computer keeps its current mode; rerunning setup never replaces its disk.

The installer pins the official Debian cloud image and verifies its SHA-512 before
boot. It generates fresh guest SSH keys, a pinned host key and a random workbench
access key locally. It refuses to replace an existing computer disk or configuration.

Watch `/var/lib/talos-computer/console.log` until cloud-init completes, then:

```bash
sudo systemctl enable --now talos-computer-api.service talos-computer-web.service
```

Configure your private HTTPS reverse proxy to `127.0.0.1:8830`. For example, on a
Tailscale host, use a dedicated Serve port so existing routes remain intact.
The configured HTTPS origin must match the actual browser origin exactly.
No public anonymous endpoint or Funnel is needed.

Add `EnvironmentFile=/etc/talos-computer-agent.env` to the agent's systemd service
and restart that agent once to load the new environment and supplementary group.
The generated file supplies:

| Setting | Meaning |
|---|---|
| `TALOS_COMPUTER_OWNER_SHA256` | Owner binding for the private Telegram link |
| `TALOS_COMPUTER_DESKTOP` | `0` for headless, `1` for desktop; matched by the backend configuration |
| `TALOS_COMPUTER_SOCKET` | Fixed local control socket |
| `TALOS_COMPUTER_ROOT` | Operator-derived capability target |
| `TALOS_COMPUTER_VIEW_URL` | Private HTTPS workbench origin |
| `TALOS_COMPUTER_VIEW_KEY_FILE` | Local key file; never model context |

Open **`/computer`** in the configured owner’s private Telegram chat. Group chats,
other allowlisted identities and lower-trust channels cannot receive the access link. Its structured reply carries
a personal link and is excluded from model memory and normal conversation archival.
The web page immediately removes the login fragment from the address bar and uses
an eight-hour Secure, HttpOnly, SameSite cookie. Keep the link private.

CLI: `talos computer status` checks local configuration/socket presence;
`talos computer open` prints the private link; `talos computer setup` shows the
provisioning steps. A present socket is not a VM health check.

## Working with the computer

Use **Expand** to give the desktop the whole window, or **Fullscreen** to use your
screen. **Fit to window** shows everything; **100–200%** zoom makes small text
readable, with scrolling inside the desktop. Your browser remembers the view and
zoom. **Restore** or Escape returns to the workspace layout. These view controls
do not change who owns the computer or interrupt the current task.

On a phone, choose **Take over → Keyboard** after the desktop connects. Tap the
desired desktop field, then use the on-screen keys, or type with your phone's
keyboard in the text box and choose **Send text**. Sending text does not press
Enter; use the separate **Enter** key when ready. Shift, Ctrl and Alt apply to the
next key only. **Hide** closes the keyboard and discards unsent text. Returning
control, pausing or losing the desktop connection disables input and clears the
text box. Keyboard text is not saved in browser preferences.

Prefer an existing skill, API or CLI when it can solve the task. Use the desktop
when an actual browser or application session helps. Describe the intended project,
action and expected result. Every Computer effect requires kernel approval,
including an `exec` that looks read-only: it can access a persistent logged-in
desktop. Unattended runs cannot approve it.

After a submitted job, use `computer_status` with `op: job` and its `job_id` to
read the durable result. A queued receipt is not completion. With the optional desktop, screenshots are read
through `op: screenshot`; files through `op: files, project: ...`.

For visual reading, use `computer_status {"op":"screenshot","question":"Read the visible total."}`.
The loop proposes `see_image` for that exact saved image before the next model call.
This is a separate, budgeted kernel step: file permissions, secret-path protection,
approval requirements, cancellation and operator corrections still apply. Omit
`question` when only the image file is needed. After a click or scroll, read its
terminal job receipt, then capture and interpret the resulting screen. Repeated
unchanged captures produce a progress warning; they never replay an action.

Dashboard previews and agent images use separate retention pools. Previews keep
their newest twelve files. Agent images remain available for at least one hour;
expired images are pruned on the next agent capture. The agent pool is limited to
128 MiB: when full, a new capture fails explicitly instead of deleting a recent
receipt. Captures include a timestamp and SHA-256 digest. These identify the image,
not proof that a task has succeeded.

The workbench's **Take over** cancels active guest action units before it enables
mouse and keyboard input. **Return to Talos** revokes desktop input before allowing
agent actions. Pause and Stop cancel actions and freeze the VM; neither means
power off or disk deletion. Returning control unfreezes the desktop without
replaying interrupted jobs. A service restart starts paused.

A verified exec job can be saved as a private routine. Read the template using
`computer_status {"op":"routine","name":"..." }`, choose a fresh operation key,
inspect the intended action and submit it through the same kernel. Saving a routine
grants no standing permission and creates no schedule.

## Boundaries

- One operator, one VM. Projects are folders, **not tenant isolation**.
- The desktop can contain authenticated sessions. Agent access must be treated as
  acting with those sessions; never place unrelated credentials in the VM.
- The guest user has no sudo. No host folders, Docker socket, SSH agent or host
  model credentials are mounted into the VM.
- QEMU runs as its own system user in a private network namespace. Direct guest
  egress is disabled. HTTP(S) uses a separate proxy allowing only globally routable
  addresses on ports 80/443, including checks of all resolved IPs.
- The model cannot choose the host, control socket, principal or workbench origin.
  OS peer credentials distinguish the agent from trusted human workbench controls.
- The shell sandbox masks the Computer control sockets. Network namespaces alone
  do not isolate UNIX sockets.
- The workbench has no model-approval endpoint. Human desktop control and kernel
  approvals remain distinct.
- `browse` remains a read-only web rendering tool.
- Runtime databases, cloud-init secrets, desktop screenshots, access links and
  operator configuration belong outside Git. Public screenshots must come from a
  deliberately empty test project and be checked before publication.

## Recovery and removal

If the backend is unavailable, inspect systemd state and the existing job before
retrying. Do not delete its database to clear a stuck task. If takeover cannot
confirm cancellation, the VM stays paused.

To disable the optional feature, stop the web/API/VM services, remove its dedicated
private reverse-proxy route and remove its environment file from the agent unit.
Retain `/var/lib/talos-computer` for recovery; disk deletion is a separate decision.
Rotating `view_secret` and the matching view key file, followed by a web service
restart, invalidates all existing access links and cookies.

## Verification

Desktop screenshots use a separate capture directory. Provisioning grants the
configured agent UID read access, including an inherited ACL for future captures;
the agent cannot write these files and other users cannot read them. This also
works when an existing systemd user manager has not picked up new group membership.
The control socket being reachable does not prove that screenshot files are readable.
When diagnosing `Permission denied`, compare the running process's `/proc/<pid>/status`
groups with `getfacl` on the capture directory and file, rather than relying on a
fresh login's `id`. Keep screenshot contents private during these checks.

Run the repository tests, adversarial suite and real-model E2E tests. For an actual
deployment also prove: guest file read-back and checksum, duplicate key non-replay,
private-network denial, peer-role denial, takeover cancelling a long-running job,
no continued write after cancellation, browser health, mobile layout and private
HTTPS authentication. A generated installer is not equivalent to a complete clean
host installation test.

## Attended computer work and forms

The default still requests approval for computer effects. An operator can set
`TALOS_COMPUTER_AUTOAPPROVE=1` in the protected service environment to permit
work in their own VM during trusted attended sessions at autonomy 5. This includes
browser interactions with external accounts; it is an explicit operator choice.
The option never grants rights to strangers, ASK channels, schedules or read-only
delegates, and never resumes human-owned control. Every action still gets a kernel
decision, capability and receipt. `TALOS_ATTENDED_AUTOAPPROVE=0` disables it too.

`TALOS_REMOTE_READONLY_AUTOAPPROVE=1` separately permits a closed grammar of basic
status commands on already configured SSH aliases. No shell chaining, arbitrary
paths, service credential properties, restart or other administration is included.
Both switches are policy settings: a model cannot enable them through config set.

The Computer now has a persistent Chromium browser with semantic form actions:
inspect, navigate, fill, type, select, check, click, submit, press, wait and upload.
Use `computer_run` with `op: browser`, a project, a stable key, title and action.
Reuse the observed `tab` identifier on subsequent actions. Chromium can reorder
numeric page indices between connections; `tab` identifies the same target across
reconnects and fails explicitly if that tab has closed. Do not send `page` and
`tab` together. Tab identity does not grant permission or bypass a kernel check.
Its job receipt lists observed selectors, required fields, options, validation errors,
page text, response statuses and console page errors. Use the returned selectors;
wait for each dependent choice and check values after blur. Uploaded files must
already exist inside that project. Password and one-time-code fields require takeover.

Headless installations can automate browser forms too. A desktop is only needed
for the live picture and human mouse/keyboard takeover. CDP listens on guest
loopback only; it is never published on the host. The browser uses the existing
egress proxy and a guest-only profile. Its Playwright dependencies are pinned and
hashed in deploy/computer-requirements.lock.

A requested submission uses the observed submit button after field validation.
A visible CAPTCHA challenge pauses submission for operator confirmation/takeover;
a passive badge is not treated as failure. This is not a paid CAPTCHA-solving
integration. After confirmation the agent must inspect the existing page and
confirm that the challenge actually cleared. Uncertain submissions are never
replayed blindly. A click or HTTP 200 alone is not proof of a completed registration.

The view-size regression can run against an installed private Computer without
changing control or sending jobs. Keep its screenshots private:

```bash
python tests/computer_view_e2e.py --config /etc/talos-computer.json \
  --evidence-dir /tmp/talos-computer-view-evidence
```

It requires Playwright and Chromium, reads the private configuration only for this
test session, and checks Expand, zoom/scroll, fullscreen, reload persistence and
mobile layout twice. `--candidate-assets` tests this checkout over the installed
backend before updating the three static workbench assets.

## Wenn der Browser nicht zurückkommt (ältere Computer)

Ein Computer, der vor 0.19.9 angelegt wurde, trägt `Restart=on-failure` in
`talos-browser.service`. Chromium beendet sich aber auch **sauber** — ein geschlossenes
Fenster, `chrome://quit`, ein aufgeräumter OOM-Abbruch liefern Exit 0, und die werden
damit nicht neu gestartet. Gemessen am 12.09.2026: der Browser starb am 7. September
und kam bis zum 12. nicht zurück; ein Auftrag lief in `ECONNREFUSED 127.0.0.1:9222`
und kostete über zwanzig Minuten Suche.

Ab 0.19.9 steht `Restart=always` in der Unit — neue Computer sind damit versorgt.

⚠️ **Ein bestehender Computer lässt sich nicht nachrüsten**, und das ist Absicht:
`deploy/computer-setup.py` weigert sich, eine vorhandene VM zu ersetzen, der Gast hat
kein Root-SSH, `desk` kein passwortloses `sudo`, und die Unit gehört root. Genau diese
Isolation ist der Sinn des Computers.

Was ohne root geht, ist ein **Netz auf Nutzerebene**:
`deploy/talos-browser-keeper.service` nach `~/.config/systemd/user/` im Gast, dann

```bash
systemctl --user daemon-reload
systemctl --user enable --now talos-browser-keeper
```

Er weicht dem System-Dienst aus, statt mit ihm um Port 9222 zu streiten: antwortet der
Port bereits, bricht der Start ab und `Restart=always` prüft zehn Sekunden später
erneut. Er übernimmt also nur, wenn wirklich niemand bedient — und benutzt dasselbe
persistente Profil, nicht `/tmp`, damit Anmeldungen einen Neustart überleben.

`loginctl show-user desk -p Linger` muss `yes` melden, sonst läuft die Nutzer-Unit
nicht ohne angemeldete Sitzung.
