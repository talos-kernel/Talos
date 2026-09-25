# Talos for Mac — local preview

Open **Talos.app**, choose **Continue with Claude**, and start chatting. The app
uses the Claude CLI login already on the Mac. If it needs a login, the sign-in
flow appears in the same window. **Other connection** opens guided setup.
The explicit connection action enables this Mac account; merely opening the
app does not grant an identity or any tool approval.

After setup, **Dashboard** shows the saved model connection, current session and
recent conversations. **History** searches the latest 100 saved exchanges from
this Mac conversation. The complete archive remains on disk and searchable
through `session_search`. It is an archive, not automatic context restoration.

## Connections

**Found on this Mac** automatically discovers Claude, Codex, Antigravity, Kimi
Code and Cline, including their standard install directories outside the GUI
PATH. It refreshes when opening Connections and through **Refresh**. Only local
status checks and credential-presence checks run; discovery does not copy
tokens, sign in, switch models or launch an agent task.

**Signed in · OAuth** is a local CLI status, not a live model probe. **OAuth
login found** means stored credentials were found; expiry/refresh and the model
connection are not verified by discovery. **Installed · sign-in not verified**
means no supported login check confirmed it. Antigravity, standalone Kimi Code
and Cline are detected but do not become new main-model adapters by discovery.
Claude and Codex retain the connection actions described below.

Inside Chat, `/model` shows numbered provider choices, followed by model choices.
Type a number and press Enter. Back, paging, Retry and Cancel use the same menu;
invalid choices stay in the menu and do not become model requests. The current
model changes only after validation succeeds. `/commands` remain available.


- **Claude**: existing official CLI login; verified before saving.
- **Codex**: OpenAI OAuth through an installed Hermes CLI. A separate
  `~/.hermes/profiles/talos-desktop` profile disables native tools and MCP. A
  missing/expired login opens the OAuth flow; a failed probe keeps your previous
  Talos model. The default Hermes configuration is not edited.
- **Ollama**: lists models on the local Ollama endpoint, then verifies the chosen
  model. Start Ollama and download a model beforehand. No API key is needed.
- **Other connection**: existing API/CLI setup, including custom model names.
- **Antigravity**: remains a separate worker, not a main model option.
- **Telegram**: optional dedicated Mac bot. Set it up, then choose **Start
  Telegram**. It runs while that session stays open. Do not reuse a bot token
  already being polled by another machine. Chat and Telegram sessions are
  mutually exclusive in this preview.
- **Computer**: opens an existing private HTTPS Computer link. Headless hosting
  and an optional desktop currently run on a separate ARM64 Linux KVM host.
  This app does not provision a Mac VM. Link query/fragment tokens are not saved
  in the app's preferences.

## Your files

Configuration, workspace, operator instructions and archive live in
`~/Library/Application Support/Talos` (private to the Mac account). Bundled
runtime code is versioned separately; app updates preserve these files.
No other Talos profile or Telegram credentials are imported. The UI is native
SwiftUI; chat and guided setup use the existing Talos CLI inside a visible PTY.
Kernel permissions and approval handling are the same as `talos chat`.
Closing the app ends its session. It installs no background service or listener.

## Building the preview

Requires Apple Silicon, macOS 14+, Xcode command-line tools, `uv`, and a portable
CPython 3.13 installation. From the repository root:

```sh
uv python install cpython-3.13.12-macos-aarch64-none --no-bin
python3 macos/build_app.py --python-root "$(uv python dir)/cpython-3.13.12-macos-aarch64-none"
```

Output: `macos/build/Talos.app`. The build packages locked Python dependencies,
SwiftTerm 1.19.0 and the existing public Talos runtime. It signs locally with
an ad-hoc signature by default. Pass `--sign` for a developer identity.
Notarization and public distribution are separate; this is a local preview,
not an App Store release. An existing output is never overwritten.

`macos/app-version.json` owns the app version/build; the core has its own version.
All helpers, including account discovery, are built from this repository, never
copied out of an installed app. `Package.resolved` is enforced. A clean tracked
tree is required; `--allow-dirty` is only for a labelled local preview. The bundled
`core-provenance.json` records source commit, input hashes, backend digest and
Python version/library hash without local paths or operator configuration.
This is a repeatable source build, not a claim of byte-identical Swift binaries
across Xcode or signing versions. Public CI builds and verifies the same bundle.

```sh
python -m pytest macos/Tests -q
python -m pytest tests -q --basetemp=/tmp/talos-core-tests
```

The short temporary path avoids macOS's Unix-socket path limit in Computer
tests. Desktop tests are separate from the core test count. The builder verifies
SwiftTerm's packaged-app resource lookup without modifying dependency source.
Dependency licenses ship inside the app.
