#!/usr/bin/env python3
"""Browser E2E against the installed service-account QMP VM Computer."""
from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "macos"))

try:
    from playwright.sync_api import expect, sync_playwright
except ModuleNotFoundError:
    # The normal unit-test environment intentionally does not install the
    # browser driver.  Require it only when the installed-VM E2E actually runs.
    expect = None
    sync_playwright = None
from talos.configcli import read_file
from talos.computer.qmp_service import framebuffer_visible

VISION_OCR_SCRIPT = Path(__file__).with_name("vision_ocr.swift")
TOKEN_FRAGMENT = re.compile(r"#token=[A-Z0-9._~%+/=-]*", re.IGNORECASE)
TOKEN_REDACTION = "#token=<redacted>"
OCR_PROOF_GRACE_S = 10.0
TYPE_TRANSPORT_TIMEOUT_S = 10
TYPE_PROOF_TIMEOUT_S = 20
TERMINAL_OPEN_TIMEOUT_S = 25
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_WIDTH = 1440
PNG_HEIGHT = 900
PNG_STRIDE = PNG_WIDTH * 3
PNG_DECOMPRESSED_SIZE = (PNG_STRIDE + 1) * PNG_HEIGHT
PNG_MAX_COMPRESSED_SIZE = 5 * 1024 * 1024
PNG_MAX_FILE_SIZE = 8 * 1024 * 1024
TERMINAL_EXIT_INPUTS = (
    {"op": "click", "x": 320, "y": 420, "button": 1},
    {"op": "type", "text": "exit"},
    {"op": "key", "keys": "Return"},
)


def require_playwright():
    """Fail explicitly when the installed-browser E2E lacks its optional driver."""
    if expect is None or sync_playwright is None:
        raise RuntimeError(
            "installed QMP E2E requires the optional Playwright dependency")


def sanitize_evidence(value, *, token=None):
    """Remove workbench credentials while retaining the surrounding diagnostic."""
    if isinstance(value, str):
        sanitized = TOKEN_FRAGMENT.sub(TOKEN_REDACTION, value)
        return sanitized.replace(token, "<redacted>") if token else sanitized
    if isinstance(value, dict):
        return {
            sanitize_evidence(key, token=token) if isinstance(key, str) else key:
            sanitize_evidence(item, token=token)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_evidence(item, token=token) for item in value]
    return value


def write_evidence_json(path, value, *, token=None):
    """Persist JSON only after recursively applying the evidence redaction boundary."""
    Path(path).write_text(
        json.dumps(sanitize_evidence(value, token=token), indent=2) + "\n",
        encoding="utf-8")


def decode_png_pixels(raw, name="capture"):
    """Strictly decode one bounded RGB8 framebuffer PNG."""
    if len(raw) > PNG_MAX_FILE_SIZE:
        raise RuntimeError(f"{name} exceeds the PNG size limit")
    if raw[:len(PNG_SIGNATURE)] != PNG_SIGNATURE:
        raise RuntimeError(f"{name} is not a PNG")
    position = len(PNG_SIGNATURE)
    chunk_index = 0
    seen_ihdr = False
    seen_idat = False
    idat_ended = False
    seen_iend = False
    compressed = bytearray()
    while position < len(raw):
        if len(raw) - position < 12:
            raise RuntimeError(f"{name} has a truncated PNG chunk")
        size = struct.unpack(">I", raw[position:position + 4])[0]
        chunk_end = position + 12 + size
        if chunk_end > len(raw):
            raise RuntimeError(f"{name} has a truncated PNG chunk")
        kind = raw[position + 4:position + 8]
        value = raw[position + 8:position + 8 + size]
        expected_crc = struct.unpack(">I", raw[position + 8 + size:chunk_end])[0]
        actual_crc = zlib.crc32(value, zlib.crc32(kind)) & 0xffffffff
        if expected_crc != actual_crc:
            raise RuntimeError(f"{name} has an invalid PNG CRC")
        if len(kind) != 4 or not all(
                65 <= byte <= 90 or 97 <= byte <= 122 for byte in kind):
            raise RuntimeError(f"{name} has an invalid PNG chunk type")
        if chunk_index == 0 and kind != b"IHDR":
            raise RuntimeError(f"{name} PNG does not begin with IHDR")
        if kind == b"IHDR":
            if seen_ihdr or chunk_index != 0 or size != 13:
                raise RuntimeError(f"{name} has an invalid PNG IHDR")
            values = struct.unpack(">IIBBBBB", value)
            if values != (PNG_WIDTH, PNG_HEIGHT, 8, 2, 0, 0, 0):
                raise RuntimeError(f"{name} is not a fixed RGB8 framebuffer PNG")
            seen_ihdr = True
        elif kind == b"IDAT":
            if not seen_ihdr or idat_ended or seen_iend:
                raise RuntimeError(f"{name} has out-of-order PNG IDAT chunks")
            if len(compressed) + size > PNG_MAX_COMPRESSED_SIZE:
                raise RuntimeError(f"{name} exceeds the compressed PNG size limit")
            compressed.extend(value)
            seen_idat = True
        elif kind == b"IEND":
            if (not seen_ihdr or not seen_idat or seen_iend or size != 0):
                raise RuntimeError(f"{name} has an invalid PNG IEND")
            seen_iend = True
            position = chunk_end
            if position != len(raw):
                raise RuntimeError(f"{name} has trailing bytes after PNG IEND")
            break
        else:
            if not seen_ihdr or seen_iend:
                raise RuntimeError(f"{name} has an out-of-order PNG chunk")
            if kind[0] & 0x20 == 0:
                raise RuntimeError(f"{name} has an unsupported critical PNG chunk")
            if seen_idat:
                idat_ended = True
        position = chunk_end
        chunk_index += 1
    if not seen_iend:
        raise RuntimeError(f"{name} PNG is missing IEND")
    decompressor = zlib.decompressobj()
    try:
        rows = decompressor.decompress(
            bytes(compressed), PNG_DECOMPRESSED_SIZE + 1)
        if len(rows) > PNG_DECOMPRESSED_SIZE or decompressor.unconsumed_tail:
            raise RuntimeError(f"{name} exceeds the decompressed PNG size limit")
        rows += decompressor.flush(PNG_DECOMPRESSED_SIZE + 1 - len(rows))
    except zlib.error as error:
        raise RuntimeError(f"{name} has invalid PNG compression") from error
    if (len(rows) != PNG_DECOMPRESSED_SIZE or not decompressor.eof
            or decompressor.unused_data):
        raise RuntimeError(f"{name} has invalid decompressed PNG data")
    result = bytearray()
    for offset in range(0, len(rows), PNG_STRIDE + 1):
        if rows[offset] != 0:
            raise RuntimeError(f"{name} uses an unsupported PNG row filter")
        result.extend(rows[offset + 1:offset + PNG_STRIDE + 1])
    return result


def pixels(path):
    path = Path(path)
    try:
        if path.stat().st_size > PNG_MAX_FILE_SIZE:
            raise RuntimeError("capture exceeds the PNG size limit")
        with path.open("rb") as stream:
            raw = stream.read(PNG_MAX_FILE_SIZE + 1)
    except OSError as error:
        raise RuntimeError("capture PNG is unavailable") from error
    return decode_png_pixels(raw)


def region_changes(before, after, *, x0=16, y0=36, x1=1424, y1=884):
    """Compare workspace content, excluding compositor chrome at the edges."""
    if len(before) != len(after):
        raise RuntimeError("framebuffer byte count changed")
    changed = 0
    for y in range(y0, y1):
        start = (y * 1440 + x0) * 3
        end = (y * 1440 + x1) * 3
        for index in range(start, end, 3):
            changed += before[index:index + 3] != after[index:index + 3]
    return changed


def normalize_ocr(value):
    """Remove intra-observation whitespace while preserving Vision line boundaries."""
    return "\n".join(
        compact for line in value.splitlines()
        if (compact := "".join(line.split())))


def ocr_has_echo_command(value, marker):
    """Recognize the complete command before Enter, including a cursor artifact."""
    lines = [line.casefold() for line in value.splitlines() if line]
    marker = marker.casefold()
    expected = "echo" + marker
    return any(
        line == expected or (
            line in {"echo", "echo|"} and index + 1 < len(lines)
            and lines[index + 1] == marker)
        for index, line in enumerate(lines))


def ocr_has_echo_output(value, marker):
    """Require an exact command followed later by its exact terminal output."""
    lines = [line.casefold() for line in value.splitlines() if line]
    marker = marker.casefold()
    expected = "echo" + marker
    command_ends = []
    for index, line in enumerate(lines):
        if line == expected:
            command_ends.append(index)
        elif line == "echo" and index + 1 < len(lines) and lines[index + 1] == marker:
            command_ends.append(index + 1)
    return any(marker in lines[end + 1:] for end in command_ends)


def vision_ocr(path, deadline, script=VISION_OCR_SCRIPT):
    """Run Vision only inside the caller's wall-clock semantic deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Vision OCR deadline expired")
    try:
        return normalize_ocr(subprocess.run(
            ["/usr/bin/swift", str(script), str(path)],
            check=True, capture_output=True, text=True,
            timeout=remaining).stdout)
    except subprocess.TimeoutExpired as error:
        raise TimeoutError("Vision OCR exceeded the semantic deadline") from error


def capture_semantic_frame(rpc, question, deadline):
    """Capture before the semantic deadline; later OCR must not move that time."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("semantic capture deadline expired")
    candidate = Path(rpc(
        "read", {"op": "screenshot", "question": question},
        timeout=min(5, remaining))["image_path"])
    captured_at = time.monotonic()
    if captured_at > deadline:
        raise TimeoutError("semantic capture completed after its deadline")
    return candidate, captured_at


def capture_semantic_probe(rpc, question, deadline, started_at, timeline):
    """Treat a transient capture timeout as a missed, never successful, probe."""
    try:
        return capture_semantic_frame(rpc, question, deadline)
    except TimeoutError:
        timeline.append({
            "elapsed_ms": round((time.monotonic() - started_at) * 1000),
            "capture_timeout": True,
        })
        return None


def terminal_launch_deadline(started_at):
    """Keep the launch bound absolute from the pre-dispatch timestamp."""
    return started_at + TERMINAL_OPEN_TIMEOUT_S


def poll_cleanup_visual_state(rpc, baseline, marker, wait, *, timeout=8,
                              question="terminal cleanup verification"):
    """Poll post-Return frames until the recorded workspace is visibly restored."""
    started_at = time.monotonic()
    deadline = started_at + timeout
    observations = []
    last = None
    while time.monotonic() < deadline:
        probe = capture_semantic_probe(
            rpc, question, deadline, started_at, observations)
        if probe is None:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                wait(min(0.25, remaining))
            continue
        candidate, captured_at = probe
        changed = region_changes(pixels(baseline), pixels(candidate))
        try:
            candidate_ocr = vision_ocr(candidate, deadline)
        except TimeoutError:
            observations.append({
                "elapsed_ms": round((captured_at - started_at) * 1000),
                "changed_pixels": changed,
                "ocr_timeout": True,
            })
            break
        marker_count = candidate_ocr.casefold().count(marker.casefold())
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        public = {
            "elapsed_ms": round((captured_at - started_at) * 1000),
            "sha256": digest,
            "marker_count": marker_count,
            "changed_pixels": changed,
        }
        observations.append(public)
        last = {
            "frame": candidate,
            "observation": public,
            "restored": marker_count == 0 and changed < 5000,
            "observations": observations,
        }
        if last["restored"]:
            return last
        remaining = deadline - time.monotonic()
        if remaining > 0:
            wait(min(0.5, remaining))
    if last is None:
        return {"frame": None, "observation": None, "restored": False,
                "observations": observations}
    return last


def dispatch_cleanup_and_verify(page, initial_inputs, receipts, observe,
                                record_receipt=None):
    """Dispatch cleanup once, record each receipt, then require visual restoration."""
    for cleanup_input in initial_inputs:
        receipt = browser_input(page, cleanup_input)
        receipts.append({"request": cleanup_input, **receipt})
        if record_receipt is not None:
            record_receipt()
        if receipt != {"status": 200,
                       "body": {"input": "dispatched", "control": "human"}}:
            raise RuntimeError("desktop cleanup transport changed")
    state = observe("after-exit")
    if not state["restored"]:
        raise RuntimeError("terminal exit did not restore the recorded baseline")
    return state


def wait_for_visible_desktop(rpc, wait, *, timeout=35):
    """Accept framebuffer readiness only when validated inside one deadline."""
    deadline = time.monotonic() + timeout
    message = "installed guest never reached a visible desktop within the deadline"
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(message)
        visible = False
        try:
            ready = Path(rpc(
                "read", {"op": "screenshot",
                         "question": "installed desktop readiness"},
                timeout=remaining)["image_path"])
            visible = framebuffer_visible(ready.read_bytes())
        except TimeoutError:
            visible = False
        except RuntimeError as error:
            if str(error) != "QMP framebuffer must be 1440x900 RGB":
                raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(message)
        if visible:
            return ready
        wait(min(0.5, remaining))


def browser_input(page, input_value, *, timeout_ms=5000):
    """One browser input POST with a real abort deadline and its exact receipt."""
    return page.evaluate("""async ({input,timeoutMs}) => {
      const controller=new AbortController();
      const timer=setTimeout(()=>controller.abort(),timeoutMs);
      try {
        const response=await fetch('/api/input',{
          method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify(input),signal:controller.signal
        });
        const body=await response.json();
        if(!response.ok)throw new Error('desktop input refused '+response.status);
        return {status:response.status,body};
      } finally { clearTimeout(timer); }
    }""", {"input": input_value, "timeoutMs": timeout_ms})


def prove_recovery_guest_output(page):
    """Cross the resume barrier before any failure-recovery input is allowed."""
    page.wait_for_timeout(1000)
    status = page.evaluate("""async () => {
      const response=await fetch('/api/screen',{cache:'no-store',
        signal:AbortSignal.timeout(16000)});
      await response.arrayBuffer();
      return response.status;
    }""")
    if status != 200:
        raise RuntimeError("failure cleanup could not prove resumed guest output")


def recover_failure_with_workbench(link, browser_path, inputs, observe=None,
                                   receipts=None, record_receipt=None):
    """Use the authenticated workbench for bounded cleanup, then prove it paused."""
    require_playwright()
    if receipts is None:
        receipts = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, executable_path=str(browser_path))
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(link, wait_until="domcontentloaded")
            expect(page.locator("#workspace")).to_be_visible(timeout=30000)
            if "token=" in page.url:
                raise RuntimeError("access token remained in cleanup browser URL")
            page.evaluate("window.__talosClearIntervals?.()")
            control = page.locator("#control-state")
            state = control.inner_text()
            if state != "Computer paused":
                if state != "You have control":
                    page.locator("#takeover").click()
                    expect(control).to_have_text("You have control", timeout=30000)
                state = "You have control"
            if inputs and state == "Computer paused":
                page.locator("#takeover").click()
                expect(control).to_have_text("You have control", timeout=30000)
                state = "You have control"
            if inputs:
                # QMP ``cont`` is an acknowledgement, not proof that the guest
                # has resumed consuming virtio-input reports.  Give the fixed
                # VM one bounded resume interval, then require one fresh QMP
                # framebuffer before the first recovery key.
                prove_recovery_guest_output(page)
            if inputs:
                if observe is None:
                    raise RuntimeError("failure cleanup has no visual-state observer")
                dispatch_cleanup_and_verify(
                    page, inputs, receipts, observe, record_receipt)
            if state != "Computer paused":
                page.locator("#pause").click()
            expect(control).to_have_text("Computer paused", timeout=10000)
        finally:
            browser.close()
    return receipts


def main():
    require_playwright()
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--marker", required=True)
    parser.add_argument("--browser", type=Path,
                        default=Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    options = parser.parse_args()
    evidence = options.evidence_dir.resolve()
    evidence.mkdir(parents=True, mode=0o700, exist_ok=False)
    errors, bad, failed, posts, input_posts = [], [], [], [], []
    terminal_state = {"may_be_open": False, "cleanup_attempted": False,
                      "cleanup_proven": False, "failure_recovery_attempted": False}
    result = {"ok": False, "phase": "initializing"}
    jobs_hash = None
    workbench_token = None
    def write_result():
        result.update(console_errors=errors, http_errors=bad,
                      request_failures=failed, post_requests=len(posts),
                      input_requests=input_posts, initial_job_hash=jobs_hash)
        write_evidence_json(evidence / "result.json", result,
                            token=workbench_token)
    atexit.register(write_result)
    original_excepthook = sys.excepthook
    def record_unhandled(kind, value, traceback):
        safe_error = sanitize_evidence(str(value), token=workbench_token)
        result.update(ok=False, error=safe_error, exception_type=kind.__name__)
        write_result()
        try:
            safe_value = kind(safe_error)
        except Exception:
            safe_value = RuntimeError(safe_error)
        original_excepthook(type(safe_value), safe_value, traceback)
    sys.excepthook = record_unhandled

    profile = options.profile.resolve(strict=True)
    values = read_file(profile / "talos.env")
    link_file = profile / "computer.url"
    if (link_file.is_symlink() or link_file.stat().st_uid != os.getuid()
            or link_file.stat().st_mode & 0o077):
        raise RuntimeError("local Computer link is not private")
    link = link_file.read_text(encoding="utf-8").strip()
    if "#token=" in link:
        workbench_token = link.split("#token=", 1)[1].split("&", 1)[0]
    if not link.startswith("http://127.0.0.1:8830/#token="):
        raise RuntimeError("local Computer link is not the fixed loopback workbench")
    socket_path, owner = values["TALOS_COMPUTER_SOCKET"], values["TALOS_COMPUTER_OWNER_SHA256"]

    def rpc(kind, args, *, timeout=20):
        with socket.socket(socket.AF_UNIX) as channel:
            channel.settimeout(timeout)
            channel.connect(socket_path)
            channel.sendall(json.dumps({"kind": kind, "owner": owner, "args": args}).encode() + b"\n")
            result = json.loads(channel.makefile("rb").readline())
        if "error" in result:
            raise RuntimeError(result["error"])
        return result

    def leave_paused():
        def workspace_restored(label):
            baseline = terminal_state.get("baseline")
            if not baseline:
                raise RuntimeError("failure cleanup has no recorded workspace baseline")
            samples = []
            result[f"failure_{label}_samples"] = samples
            for index in range(2):
                restored = Path(rpc(
                    "read", {"op": "screenshot",
                             "question": "failure cleanup verification"},
                    timeout=5)["image_path"])
                target = evidence / f"failure-{label}-{index + 1}.png"
                shutil.copy2(restored, target)
                restored_ocr = vision_ocr(restored, time.monotonic() + 10)
                samples.append({
                    "evidence": target.name,
                    "sha256": hashlib.sha256(restored.read_bytes()).hexdigest(),
                    "marker_count": restored_ocr.casefold().count(
                        options.marker.casefold()),
                    "changed_pixels": region_changes(pixels(baseline), pixels(restored)),
                })
                if index == 0:
                    time.sleep(0.5)
            return all(sample["marker_count"] == 0
                       and sample["changed_pixels"] < 5000 for sample in samples)

        def observe_failure_cleanup(label):
            baseline = terminal_state.get("baseline")
            if not baseline:
                raise RuntimeError("failure cleanup has no recorded workspace baseline")
            state = poll_cleanup_visual_state(
                rpc, Path(baseline), options.marker, time.sleep,
                question=f"failure cleanup {label}")
            result[f"failure_{label}_timeline"] = state["observations"]
            if state["frame"] is not None:
                target = evidence / f"failure-{label}-decision.png"
                shutil.copy2(state["frame"], target)
                state["observation"] = {
                    **state["observation"], "evidence": target.name}
                result[f"failure_{label}_decision"] = state["observation"]
            return state

        cleanup = []
        result["failure_terminal_cleanup"] = cleanup
        def record_failure_cleanup_receipt():
            write_evidence_json(
                evidence / "failure-cleanup-transport.json", cleanup,
                token=workbench_token)
        try:
            status = rpc("read", {"op": "status"}, timeout=5)
            if terminal_state["may_be_open"]:
                try:
                    terminal_state["cleanup_proven"] = workspace_restored("precheck")
                except Exception as error:
                    # A full or temporarily unavailable capture store must not
                    # prevent the one bounded terminal recovery attempt.
                    result["failure_precheck_error"] = str(error)
                    terminal_state["cleanup_proven"] = False
            if (terminal_state["may_be_open"]
                    and not terminal_state["cleanup_proven"]):
                if terminal_state["cleanup_attempted"]:
                    raise RuntimeError(
                        "terminal cleanup was already dispatched; refusing to retry")
                if terminal_state["failure_recovery_attempted"]:
                    raise RuntimeError("failure recovery sequence was already attempted")
                terminal_state["failure_recovery_attempted"] = True
                result["failure_recovery_attempted"] = True
                cleanup_inputs = (
                    TERMINAL_EXIT_INPUTS[0],
                    {"op": "key", "keys": "ctrl+c"},
                    *TERMINAL_EXIT_INPUTS[1:],
                )
                recover_failure_with_workbench(
                    link, options.browser, cleanup_inputs,
                    observe_failure_cleanup, receipts=cleanup,
                    record_receipt=record_failure_cleanup_receipt)
                terminal_state["cleanup_proven"] = workspace_restored("recovery")
            result["failure_terminal_cleanup_proven"] = terminal_state["cleanup_proven"]
            if terminal_state["cleanup_proven"]:
                terminal_state["may_be_open"] = False
            elif terminal_state["may_be_open"]:
                result["failure_terminal_cleanup_error"] = (
                    "terminal did not return to the recorded workspace baseline")
        except Exception as error:
            result["failure_terminal_cleanup_error"] = str(error)
        try:
            status = rpc("read", {"op": "status"}, timeout=5)
            if status["control"] != "paused" or status["vm"] != "paused":
                recover_failure_with_workbench(link, options.browser, ())
            final = rpc("read", {"op": "status"}, timeout=5)
            result.update(failure_cleanup_control=final["control"],
                          failure_cleanup_vm=final["vm"])
            if final["control"] != "paused" or final["vm"] != "paused":
                result["failure_cleanup_error"] = "pause read-back did not reach paused/paused"
        except Exception as error:
            result["failure_cleanup_error"] = str(error)
    atexit.register(leave_paused)
    initial = rpc("read", {"op": "status"})
    if initial["control"] != "paused" or initial["vm"] != "paused":
        raise RuntimeError("installed E2E must begin fail-closed")
    initial_jobs = initial["jobs"]
    jobs_hash = hashlib.sha256(json.dumps(
        initial_jobs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=str(options.browser))
        def close_browser():
            try:
                browser.close()
            except Exception as error:
                result["browser_close_error"] = str(error)
        atexit.register(close_browser)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        context.add_init_script("""(() => {
          const original=window.setInterval.bind(window), intervals=[];
          window.setInterval=(...args)=>{const id=original(...args);intervals.push(id);return id;};
          window.__talosClearIntervals=()=>{for(const id of intervals)clearInterval(id);intervals.length=0;};
        })();""")
        page = context.new_page()
        pending = set()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        page.on("response", lambda response: bad.append((response.status, response.url))
                if response.status >= 400 else None)
        def request_failed(request):
            pending.discard(request)
            failed.append({"url": request.url, "reason": request.failure})
        page.on("requestfailed", request_failed)
        page.on("requestfinished", lambda request: pending.discard(request))
        def record_request(request):
            pending.add(request)
            if request.method != "POST":
                return
            posts.append(request.url)
            if request.url.endswith("/api/input"):
                input_posts.append(request.post_data_json)
        page.on("request", record_request)
        page.goto(link, wait_until="domcontentloaded")
        expect(page.locator("#workspace")).to_be_visible(timeout=30000)
        if "token=" in page.url:
            raise RuntimeError("access token remained in browser URL")
        expect(page.locator("#control-state")).to_have_text("Computer paused")
        page.locator("#takeover").click()
        expect(page.locator("#control-state")).to_have_text("You have control", timeout=30000)
        page.wait_for_function("() => document.getElementById('capture').naturalWidth===1440",
                               timeout=30000)
        wait_for_visible_desktop(
            rpc, lambda delay: page.wait_for_timeout(delay * 1000))
        # The snapshot workbench refreshes through the same QMP capture source.
        # Stop its periodic preview after readiness so semantic evidence owns the
        # transport instead of racing a decorative refresh.
        page.evaluate("window.__talosClearIntervals()")
        preview_drain_deadline = time.monotonic() + 10
        while pending and time.monotonic() < preview_drain_deadline:
            page.wait_for_timeout(50)
        if pending:
            raise RuntimeError(
                f"workbench preview requests did not drain: {len(pending)}")
        # Establish a visual workspace baseline so this trial can prove that the
        # terminal it opens later disappears again.
        terminal_launch_inputs = (
            # Workspace 5 is reserved for qualification. This installed image
            # visibly binds its terminal to Super+Return; Super+Space is its launcher.
            {"op": "click", "x": 130, "y": 13, "button": 1},
            {"op": "key", "keys": "super+Return"},
        )
        empty_workspace = None
        terminal_launch_started = None
        for index, input_value in enumerate(terminal_launch_inputs):
            if index == 1:
                terminal_state["may_be_open"] = True
                terminal_launch_started = time.monotonic()
            receipt = browser_input(page, input_value)
            if receipt != {"status": 200, "body": {"input": "dispatched", "control": "human"}}:
                raise RuntimeError(f"desktop setup transport changed: {receipt}")
            if index == 0:
                page.wait_for_timeout(500)
                empty_workspace = Path(rpc(
                    "read", {"op": "screenshot", "question": "empty qualification workspace"},
                    timeout=5)["image_path"])
                terminal_state["baseline"] = str(empty_workspace)
                baseline_target = evidence / "00-empty-workspace.png"
                shutil.copy2(empty_workspace, baseline_target)
                baseline_ocr = vision_ocr(empty_workspace, time.monotonic() + 10)
                (evidence / "00-empty-workspace-ocr.txt").write_text(
                    baseline_ocr, encoding="utf-8")
                baseline_marker_count = baseline_ocr.casefold().count(
                    options.marker.casefold())
                result.update(
                    baseline_sha256=hashlib.sha256(
                        empty_workspace.read_bytes()).hexdigest(),
                    baseline_marker_count=baseline_marker_count,
                )
                if baseline_marker_count:
                    raise RuntimeError(
                        "qualification marker was already visible in the baseline")
        terminal_open_deadline = terminal_launch_deadline(
            terminal_launch_started)
        terminal_open_frame = None
        terminal_open_changed = -1
        while time.monotonic() < terminal_open_deadline:
            try:
                candidate, captured_at = capture_semantic_frame(
                    rpc, "waiting for installed terminal", terminal_open_deadline)
            except TimeoutError:
                continue
            terminal_open_changed = region_changes(
                pixels(empty_workspace), pixels(candidate))
            if terminal_open_changed >= 50000:
                terminal_open_frame = candidate
                result["terminal_launch_latency_ms"] = round(
                    (captured_at - terminal_launch_started) * 1000)
                break
            remaining_ms = int(max(
                0, (terminal_open_deadline - time.monotonic()) * 1000))
            if remaining_ms:
                page.wait_for_timeout(min(250, remaining_ms))
        if terminal_open_frame is None:
            raise RuntimeError(
                "installed terminal was not visibly open within 25 seconds")
        terminal_open_target = evidence / "00-terminal-open.png"
        shutil.copy2(terminal_open_frame, terminal_open_target)
        result.update(
            terminal_open_changed_pixels=terminal_open_changed,
            terminal_open_sha256=hashlib.sha256(
                terminal_open_frame.read_bytes()).hexdigest(),
        )
        focus_input = {"op": "click", "x": 320, "y": 420, "button": 1}
        focus_receipt = browser_input(page, focus_input)
        if focus_receipt != {"status": 200,
                             "body": {"input": "dispatched", "control": "human"}}:
            raise RuntimeError(f"desktop focus transport changed: {focus_receipt}")
        terminal_open_inputs = (*terminal_launch_inputs, focus_input)
        # QMP VM's first shell can remain in startup with only its window frame
        # visible. One bounded interrupt produces the prompt without executing
        # content, and its receipt remains part of the audited setup sequence.
        prompt_input = {"op": "key", "keys": "ctrl+c"}
        prompt_receipt = browser_input(page, prompt_input)
        if prompt_receipt != {"status": 200,
                              "body": {"input": "dispatched",
                                       "control": "human"}}:
            raise RuntimeError(f"desktop prompt transport changed: {prompt_receipt}")
        setup_inputs = (*terminal_open_inputs, prompt_input)
        page.wait_for_timeout(1500)
        focused = Path(rpc("read", {"op": "screenshot",
                                     "question": "focused terminal before typing"})["image_path"])
        focused_target = evidence / "00-focused-terminal.png"
        shutil.copy2(focused, focused_target)
        focused_terminal_changed = region_changes(
            pixels(empty_workspace), pixels(focused))
        if focused_terminal_changed < 50000:
            raise RuntimeError(
                "opened terminal did not differ sufficiently from baseline "
                f"({focused_terminal_changed} pixels)")
        result.update(
            focused_terminal_changed_pixels=focused_terminal_changed,
            focused_terminal_sha256=hashlib.sha256(
                focused.read_bytes()).hexdigest(),
        )
        page.locator("#keyboard-toggle").click()
        typed_text = "echo " + options.marker
        page.locator("#keyboard-text").fill(typed_text)
        result["phase"] = "type_transport"
        type_started = time.monotonic()
        type_deadline = type_started + TYPE_PROOF_TIMEOUT_S
        with page.expect_response(
                lambda response: response.url.endswith("/api/input")
                and response.request.method == "POST",
                timeout=TYPE_TRANSPORT_TIMEOUT_S * 1000) as type_info:
            page.locator("#keyboard-send").click()
        type_response = type_info.value
        type_receipt = {
            "request": type_response.request.post_data_json,
            "status": type_response.status,
            "body": type_response.json(),
        }
        write_evidence_json(evidence / "type-transport.json", type_receipt,
                            token=workbench_token)
        result["type_transport"] = type_receipt
        if type_receipt != {"request": {"op": "type", "text": typed_text},
                            "status": 200,
                            "body": {"input": "dispatched", "control": "human"}}:
            raise RuntimeError(f"installed type transport changed: {type_receipt}")
        expected_type = {"op": "type", "text": typed_text}
        if input_posts != [*setup_inputs, expected_type]:
            raise RuntimeError(f"installed type request sequence changed: {input_posts}")
        remaining_ms = int(max(1, (type_deadline - time.monotonic()) * 1000))
        expect(page.locator("#keyboard-text")).to_have_value("", timeout=remaining_ms)
        result["phase"] = "type_semantics"
        typed, typed_ocr, timeline, last_candidate = None, "", [], None
        last_hash = None
        while time.monotonic() < type_deadline:
            probe = capture_semantic_probe(
                rpc, "before installed Enter", type_deadline,
                type_started, timeline)
            if probe is None:
                remaining_ms = int(max(
                    0, (type_deadline - time.monotonic()) * 1000))
                if remaining_ms:
                    page.wait_for_timeout(min(250, remaining_ms))
                continue
            candidate, captured_at = probe
            last_candidate = candidate
            if not timeline:
                shutil.copy2(candidate, evidence / "01-type-immediate.png")
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
            if digest != last_hash:
                try:
                    candidate_ocr = vision_ocr(
                        candidate, type_deadline + OCR_PROOF_GRACE_S)
                except TimeoutError:
                    timeline.append({
                        "elapsed_ms": round((time.monotonic() - type_started) * 1000),
                        "sha256": digest, "ocr_timeout": True})
                    break
                sample = evidence / f"01-type-sample-{len(timeline):02d}.png"
                shutil.copy2(candidate, sample)
                marker_count = candidate_ocr.casefold().count(options.marker.casefold())
                command_complete = ocr_has_echo_command(
                    candidate_ocr, options.marker)
                elapsed_ms = round((captured_at - type_started) * 1000)
                timeline.append({"elapsed_ms": elapsed_ms, "evidence": sample.name,
                                 "sha256": digest, "marker_count": marker_count,
                                 "command_complete": command_complete})
                last_hash = digest
                typed_ocr = candidate_ocr
                if marker_count == 1 and command_complete:
                    typed, typed_ocr = candidate, candidate_ocr
                    break
            remaining_ms = int(max(0, (type_deadline - time.monotonic()) * 1000))
            if remaining_ms:
                page.wait_for_timeout(min(250, remaining_ms))
        write_evidence_json(evidence / "type-timeline.json", timeline,
                            token=workbench_token)
        if last_candidate is not None:
            shutil.copy2(last_candidate, evidence / "01-type-final.png")
        if typed is not None:
            shutil.copy2(typed, evidence / "01-before-enter.png")
        (evidence / "typed-ocr.txt").write_text(typed_ocr, encoding="utf-8")
        if typed is None:
            message = (
                "complete marker was not visible within "
                f"{TYPE_PROOF_TIMEOUT_S} seconds after type dispatch")
            result.update(phase="type_semantics_failed", error=message,
                          marker_count_before=max((entry.get("marker_count", 0) for entry in timeline),
                                                  default=0), type_timeline=timeline,
                          post_requests=len(posts), console_errors=errors, http_errors=bad)
            write_result()
            raise RuntimeError(message)
        result.update(marker_count_before=1, type_latency_ms=timeline[-1]["elapsed_ms"])
        expected_return = {"op": "key", "keys": "Return"}
        before = pixels(typed)
        entered = evidence / "02-after-enter.png"
        changed = -1
        ocr = ""
        ocr_sample = None
        return_timeline = []
        return_succeeded = False
        result["phase"] = "return_transport"
        return_started = time.monotonic()
        semantic_deadline = return_started + 15
        try:
            with page.expect_response(
                    lambda response: response.url.endswith("/api/input")
                    and response.request.method == "POST", timeout=15000) as response_info:
                page.locator('[data-key="Enter"]').click()
            response = response_info.value
            return_receipt = {
                "request": response.request.post_data_json,
                "status": response.status,
                "body": response.json(),
            }
            write_evidence_json(evidence / "return-transport.json", return_receipt,
                                token=workbench_token)
            result["return_transport"] = return_receipt
            if return_receipt != {"request": expected_return, "status": 200,
                                   "body": {"input": "dispatched", "control": "human"}}:
                raise RuntimeError(f"installed Enter transport changed: {return_receipt}")
        except Exception as error:
            result.update(phase="return_transport_failed", error=str(error),
                          post_requests=len(posts), console_errors=errors,
                          http_errors=bad)
            write_result()
            raise
        result["phase"] = "return_semantics"
        while time.monotonic() < semantic_deadline:
            remaining_ms = int(max(0, (semantic_deadline - time.monotonic()) * 1000))
            if remaining_ms:
                page.wait_for_timeout(min(750, remaining_ms))
            remaining = semantic_deadline - time.monotonic()
            if remaining <= 0:
                break
            probe = capture_semantic_probe(
                rpc, "after installed Enter", semantic_deadline,
                return_started, return_timeline)
            if probe is None:
                continue
            candidate, captured_at = probe
            after = pixels(candidate)
            candidate_changed = sum(before[index:index + 3] != after[index:index + 3]
                                    for index in range(0, len(before), 3))
            entry = {"elapsed_ms": round((time.monotonic() - return_started) * 1000),
                     "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
                     "changed_pixels": candidate_changed}
            sample = evidence / f"02-return-sample-{len(return_timeline):02d}.png"
            shutil.copy2(candidate, sample)
            entry["evidence"] = sample.name
            if candidate_changed > changed:
                changed = candidate_changed
                shutil.copy2(candidate, entered)
            if candidate_changed >= 150:
                try:
                    ocr = vision_ocr(
                        candidate, semantic_deadline + OCR_PROOF_GRACE_S)
                except TimeoutError:
                    entry["ocr_timeout"] = True
                    return_timeline.append(entry)
                    break
                ocr_sample = candidate
                entry["marker_count"] = ocr.casefold().count(options.marker.casefold())
                entry["output_complete"] = ocr_has_echo_output(
                    ocr, options.marker)
                entry["elapsed_ms"] = round(
                    (captured_at - return_started) * 1000)
                if entry["output_complete"]:
                    changed = candidate_changed
                    shutil.copy2(candidate, entered)
                    return_succeeded = True
                    return_timeline.append(entry)
                    break
            return_timeline.append(entry)
        write_evidence_json(evidence / "return-timeline.json", return_timeline,
                            token=workbench_token)
        if ocr_sample is not None:
            shutil.copy2(ocr_sample, evidence / "02-ocr-frame.png")
            result["return_ocr_frame_sha256"] = hashlib.sha256(
                ocr_sample.read_bytes()).hexdigest()
        (evidence / "ocr.txt").write_text(ocr, encoding="utf-8")
        if changed < 150:
            message = f"installed Enter produced no visible change ({changed} pixels)"
            result.update(phase="return_semantics_failed", error=message,
                          visible_changed_pixels=changed, post_requests=len(posts),
                          console_errors=errors, http_errors=bad)
            write_result()
            raise RuntimeError(message)
        if not return_succeeded:
            message = "installed marker is not visible as both command and terminal output"
            result.update(phase="return_semantics_failed", error=message,
                          visible_changed_pixels=changed, post_requests=len(posts),
                          console_errors=errors, http_errors=bad)
            write_result()
            raise RuntimeError(message)
        if input_posts != [*setup_inputs, expected_type, expected_return]:
            raise RuntimeError(f"installed Enter request sequence changed: {input_posts}")
        trial_input_requests = list(input_posts)
        result["trial_input_requests"] = trial_input_requests
        page.screenshot(path=str(evidence / "03-workbench.png"), full_page=True)
        if rpc("read", {"op": "status"})["jobs"] != initial_jobs:
            raise RuntimeError("operator input changed the durable agent job set")
        # Clean only after the tested command is proven to have completed. Each
        # cleanup action is still sent once and checked; it never retries an
        # uncertain Return from the trial itself.
        # Exit only the proven disposable shell. If that does not visibly restore
        # the recorded workspace, fail without sending any compositor close key.
        cleanup_inputs = TERMINAL_EXIT_INPUTS
        terminal_state["cleanup_attempted"] = True
        cleanup_decisions = []
        cleanup_receipts = []
        result["cleanup_transport_receipts"] = cleanup_receipts
        result["cleanup_decisions"] = cleanup_decisions
        def record_success_cleanup_receipt():
            write_evidence_json(
                evidence / "cleanup-transport.json", cleanup_receipts,
                token=workbench_token)
        def observe_success_cleanup(label):
            state = poll_cleanup_visual_state(
                rpc, empty_workspace, options.marker,
                lambda delay: page.wait_for_timeout(delay * 1000),
                question=f"installed E2E cleanup {label}")
            if state["frame"] is not None:
                target = evidence / f"05-cleanup-{label}.png"
                shutil.copy2(state["frame"], target)
                state["observation"] = {
                    **state["observation"], "evidence": target.name}
            cleanup_decisions.append({
                "stage": label,
                "restored": state["restored"],
                "observation": state["observation"],
                "samples": state["observations"],
            })
            write_evidence_json(evidence / "cleanup-decision-timeline.json",
                                cleanup_decisions, token=workbench_token)
            return state
        dispatch_cleanup_and_verify(
            page, cleanup_inputs, cleanup_receipts, observe_success_cleanup,
            record_success_cleanup_receipt)
        complete_cleanup_inputs = tuple(
            receipt["request"] for receipt in cleanup_receipts)
        if input_posts != [*setup_inputs, expected_type, expected_return,
                           *complete_cleanup_inputs]:
            raise RuntimeError(f"installed cleanup request sequence changed: {input_posts}")
        cleanup_samples = []
        for index in range(2):
            cleaned = Path(rpc("read", {"op": "screenshot",
                                        "question": "installed E2E cleanup"},
                               timeout=5)["image_path"])
            target = evidence / f"05-cleanup-{index + 1}.png"
            shutil.copy2(cleaned, target)
            cleaned_ocr = normalize_ocr(subprocess.run(
                ["/usr/bin/swift", str(VISION_OCR_SCRIPT), str(cleaned)],
                check=True, capture_output=True, text=True,
                timeout=10).stdout)
            (evidence / f"cleanup-ocr-{index + 1}.txt").write_text(
                cleaned_ocr, encoding="utf-8")
            cleanup_changed = region_changes(pixels(empty_workspace), pixels(cleaned))
            cleanup_samples.append({
                "evidence": target.name,
                "sha256": hashlib.sha256(cleaned.read_bytes()).hexdigest(),
                "marker_count": cleaned_ocr.casefold().count(options.marker.casefold()),
                "changed_pixels": cleanup_changed,
            })
            if index == 0:
                page.wait_for_timeout(500)
        write_evidence_json(evidence / "cleanup-timeline.json", cleanup_samples,
                            token=workbench_token)
        if any(sample["marker_count"] for sample in cleanup_samples):
            raise RuntimeError("installed E2E terminal identity remained after cleanup")
        if any(sample["changed_pixels"] >= 5000 for sample in cleanup_samples):
            raise RuntimeError(
                f"installed E2E workspace did not return to its baseline: {cleanup_samples}")
        terminal_state["may_be_open"] = False
        terminal_state["cleanup_proven"] = True
        result.update(terminal_cleanup=True, cleanup_samples=cleanup_samples)
        page.locator("#release").click()
        expect(page.locator("#control-state")).to_have_text("Talos has control")
        page.locator("#pause").click()
        expect(page.locator("#control-state")).to_have_text("Computer paused")
        final_status = rpc("read", {"op": "status"})
        result.update(final_control=final_status.get("control"),
                      final_vm=final_status.get("vm"),
                      final_job_count=len(final_status.get("jobs", [])))
        if isinstance(final_status.get("jobs"), list):
            final_jobs_hash = hashlib.sha256(json.dumps(
                final_status["jobs"], sort_keys=True,
                separators=(",", ":")).encode()).hexdigest()
            result["final_job_hash"] = final_jobs_hash
        write_result()
        if (final_status["control"], final_status["vm"], final_status["jobs"]) != (
                "paused", "paused", initial_jobs):
            raise RuntimeError("independent final pause or job-state read-back failed")
        if final_jobs_hash != jobs_hash:
            raise RuntimeError("final durable job ledger hash changed")

        # Exercise a fresh application load after the final pause, then prove the
        # browser has no hidden request, console or HTTP failure before shutdown.
        page.reload(wait_until="networkidle", timeout=10000)
        expect(page.locator("#control-state")).to_have_text("Computer paused", timeout=5000)
        if "token=" in page.url:
            raise RuntimeError("access token returned to browser URL after reload")
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator("#screen-zoom").select_option("fit")
        overflow = page.evaluate("document.documentElement.scrollWidth")
        if overflow > 390:
            raise RuntimeError(f"installed mobile workbench overflows: {overflow}")
        page.screenshot(path=str(evidence / "04-mobile.png"), full_page=True)

        page.evaluate("window.__talosClearIntervals()")
        page.goto("about:blank", wait_until="load", timeout=5000)
        drain_deadline = time.monotonic() + 5
        while pending and time.monotonic() < drain_deadline:
            page.wait_for_timeout(50)
        if pending:
            raise RuntimeError(f"browser requests remained in flight after teardown: {len(pending)}")
        context.close()
        time.sleep(0.1)
        if pending:
            raise RuntimeError(
                f"browser requests appeared during context closure: {len(pending)}")
        close_browser()
        atexit.unregister(close_browser)
        if errors or bad or failed or result.get("browser_close_error"):
            raise RuntimeError(
                f"browser health failed: console={errors}, http={bad}, requests={failed}, "
                f"close={result.get('browser_close_error')}")
        return_request_count = sum(
            request == expected_return for request in trial_input_requests)
        if return_request_count != 1:
            raise RuntimeError(f"Return request count changed: {return_request_count}")
    result.update(ok=True, phase="complete", visible_changed_pixels=changed,
                  return_latency_ms=return_timeline[-1]["elapsed_ms"],
                  console_errors=errors, http_errors=bad,
                  post_requests=len(posts), token_removed_from_url=True,
                  operator_input_without_agent_job=True,
                  mobile_no_overflow=True, visible_marker_ocr=True,
                  return_request_count=return_request_count,
                  final_control=final_status["control"], final_vm=final_status["vm"],
                  final_job_count=len(final_status["jobs"]),
                  final_job_hash=final_jobs_hash)
    write_result()
    atexit.unregister(write_result)
    atexit.unregister(leave_paused)
    sys.excepthook = original_excepthook
    print(json.dumps(sanitize_evidence(result, token=workbench_token)))


if __name__ == "__main__":
    main()
