"""Explicit, attended connection recipes. Failed probes preserve current settings."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys


def save_connection(profile: Path, values: dict) -> None:
    from talos.configcli import read_file, write_keys
    current = read_file(profile / "talos.env")
    allowed = current.get("TALOS_ALLOWED_PRINCIPALS", "").replace(",", " ").split()
    values["TALOS_ALLOWED_PRINCIPALS"] = ",".join(dict.fromkeys([*allowed, f"cli:{os.getuid()}"]))
    # Chat restores /model selections from the event log before defaults. An
    # explicit, successfully probed desktop choice must supersede that selection.
    from talos.eventlog import Event, EventLog, new_run_id
    from desktop_runtime import private_path
    private_path(profile / "data", directory=True)
    (profile / "data").mkdir(exist_ok=True, mode=0o700)
    log = EventLog(profile / "data/eventlog.db")
    try:
        write_keys(profile / "talos.env", values)
        log.append(Event(new_run_id(), "human", "model.selected", {
            "provider": values["TALOS_MODEL_PROVIDER"], "model": values["TALOS_MODEL"],
            "principal": f"cli:{os.getuid()}"}))
    finally:
        log.close()


def validate_values(values: dict) -> None:
    from talos.api_reasoner import ApiReasoner, SUPPORTED_PROVIDERS
    from talos.credentials import from_lookup
    from talos.reasoner import ClaudeCliReasoner, HermesCliReasoner
    provider, model = values.get("TALOS_MODEL_PROVIDER", ""), values.get("TALOS_MODEL", "")
    if provider in SUPPORTED_PROVIDERS:
        reasoner = ApiReasoner(provider, model, from_lookup(values.get), timeout_s=120)
    elif provider == "claude-cli":
        reasoner = ClaudeCliReasoner(values.get("TALOS_CLAUDE_BIN") or shutil.which("claude"), 120, model=model)
    else:
        reasoner = HermesCliReasoner(values.get("TALOS_HERMES_BIN") or shutil.which("hermes"), 120, provider=provider, model=model)
    reasoner.validate()


def choose_model(names: tuple[str, ...], default: str = "") -> str:
    from talos.models import is_model_id
    for index, name in enumerate(names, 1):
        print(f"  {index}. {name}")
    choice = input(f"\n  Model [number or name{', Enter = ' + default if default else ''}]: ").strip() or default
    if choice.isdigit() and 1 <= int(choice) <= len(names):
        choice = names[int(choice) - 1]
    if not is_model_id(choice):
        raise RuntimeError("Choose a valid model name. Your connection was not changed.")
    return choice


def ollama(profile: Path) -> None:
    import requests
    from talos import catalog, models
    from talos.api_reasoner import ApiReasoner
    from talos.credentials import from_lookup
    base = catalog.get("ollama").base_url
    print("\n  Ollama on this Mac\n  Start Ollama with at least one downloaded model, then choose it here.\n", flush=True)
    try:
        response = requests.get(base.rstrip("/") + "/models", timeout=5)
        response.raise_for_status()
        payload = response.json()
        names = models.clean_names(item.get("id", "") for item in payload.get("data", []) if isinstance(item, dict))
    except (requests.RequestException, ValueError, AttributeError) as error:
        raise RuntimeError("Ollama is not responding on this Mac. Open Ollama, then try again. Your model is unchanged.") from error
    if not names:
        raise RuntimeError("Ollama is running but has no models. Download a model in Ollama first.")
    model = choose_model(names, names[0])
    print("\n  Checking the selected local model…", flush=True)
    reasoner = ApiReasoner("ollama", model, from_lookup(lambda key: ""), timeout_s=120)
    answer = reasoner.reason("Connection check. Reply exactly TALOS_DESKTOP_READY. No tools.")
    if "TALOS_DESKTOP_READY" not in answer or "Reasoner error" in answer:
        raise RuntimeError("The local model did not pass its connection check. Your previous model is unchanged.")
    save_connection(profile, {"TALOS_MODEL_PROVIDER": "ollama", "TALOS_MODEL": model})
    print("\n  ✓ Local model connected.\n", flush=True)


def hermes_wrapper(profile: Path, binary: str) -> Path:
    """A dedicated Hermes profile uses its supported shared OAuth fallback.

    No default Hermes tools/config are changed. Its existing preflight must
    independently confirm this profile exposes no tools before it can reason.
    """
    from desktop_runtime import private_path
    isolated = Path.home() / ".hermes/profiles/talos-desktop"
    private_path(isolated, directory=True)
    isolated.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = isolated / "config.yaml"
    private_path(config)
    if not config.exists():
        with config.open("x") as stream:
            stream.write("toolsets: []\nplatform_toolsets:\n  cli: [no_mcp]\nmcp_servers: {}\n")
        config.chmod(0o600)
    bindir = profile / "bin"
    private_path(bindir, directory=True)
    bindir.mkdir(exist_ok=True, mode=0o700)
    wrapper = bindir / "hermes-reasoner"
    private_path(wrapper)
    command = [sys.executable, "-B", str(Path(__file__).with_name("desktop_hermes.py")), binary, str(isolated)]
    wrapper.write_text("#!/bin/sh\nexec " + shlex.join(command) + ' "$@"\n')
    wrapper.chmod(0o700)
    return wrapper


def codex(profile: Path) -> None:
    from talos.reasoner import HermesCliReasoner
    binary = shutil.which("hermes")
    if not binary:
        raise RuntimeError("Codex OAuth currently needs the Hermes CLI. Install and sign in to Hermes, then return here.")
    print("\n  Codex — your existing OAuth account\n  Uses a separate inference-only Hermes profile.\n", flush=True)
    model = choose_model(("gpt-6-astra", "gpt-5.6-sol"), "gpt-5.6-sol")
    wrapper = hermes_wrapper(profile, binary)
    reasoner = HermesCliReasoner(str(wrapper), 120, provider="openai-codex", model=model)
    print("\n  Checking your Codex connection…", flush=True)
    try:
        reasoner.validate()
    except RuntimeError as error:
        if not any(text in str(error).lower() for text in ("no codex credentials", "refresh token", "relogin", "signing in again")):
            raise
        print("\n  Sign in to Codex in the flow below, then return here.\n", flush=True)
        if subprocess.run([str(wrapper), "auth", "add", "openai-codex", "--type", "oauth"]).returncode:
            raise RuntimeError("Codex sign-in was not completed. Your current model is unchanged.")
        # Login may update provider settings. Re-prove tool isolation as well as auth.
        reasoner = HermesCliReasoner(str(wrapper), 120, provider="openai-codex", model=model)
        reasoner.validate()
    save_connection(profile, {"TALOS_MODEL_PROVIDER": "openai-codex", "TALOS_MODEL": model,
                              "TALOS_HERMES_BIN": str(wrapper)})
    print("\n  ✓ Codex connected through OAuth.\n", flush=True)
