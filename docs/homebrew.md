# Homebrew (Apple Silicon, macOS 14+)

Talos's own tap packages the beta CLI, not a notarized desktop application or a
Homebrew/core formula. Intel Macs and Linux are not supported by this formula;
use the signed installer for the other supported environments.

Homebrew 7 or newer is required. Run `brew update` before installing with an older
Homebrew installation.

```sh
brew install talos-kernel/tap/talos
talos setup terminal
talos chat
```

The source archive is pinned by SHA256. Homebrew fetches individually pinned original
wheel files from PyPI, then installs these staged files offline. Each wheel is checked
against the hashed lock in the release. Fetching the files still requires the network.
Installing the formula does not configure identities, start Talos or enable services.
Third-party dependencies retain their own licenses; the tap does not redistribute a
combined wheel bundle or a prebuilt Talos bottle. The repository's bundle-based formula
renderer is a packaging helper, not the published tap's resource manifest.

## Code and private state are separate

Homebrew owns the code in its versioned Cellar. The invoking user's private state is
`~/.talos/homebrew`: configuration, identity files, event log, selected model, projects,
workspace and local vault. The profile and its data/workspace directories are 0700;
new identity/configuration files are 0600. A shared or symlinked profile is refused,
not silently repaired. Other Talos installations are not imported automatically.

Diagnostic commands do not create a new profile. Explicit setup writes configuration;
starting the agent seeds only missing identity templates. Existing operator files are
preserved. Advanced operators may set an absolute `TALOS_PROFILE_DIR` in the launching
environment; this is policy, not a model-editable setting.

## Upgrade, removal and recovery

Stop any running Talos process, keep a private backup of the profile, then:

```sh
brew update
brew upgrade talos-kernel/tap/talos
```

The built-in `talos update` refuses a Homebrew-managed installation, even with a
different `--prefix`: one package manager owns code replacement. Removing the formula
with `brew uninstall talos-kernel/tap/talos` leaves the profile intact.

Switching code versions does not restore or erase user data. It is not a database
downgrade mechanism: check version-specific migration compatibility before running an
older release against newer state. Never overwrite new events with an old backup just
to roll back code.
