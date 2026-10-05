"""Visual QMP VM desktop behind Talos' normal Computer capability boundary.

The composition root supplies a fixed VM transport and a guest-only capture source.
Register ``runner`` only behind GrantedRunner. This module never mints permission,
opens a host shell, selects a host window, or accepts an endpoint from model text.
The composition root must place QEMU, its disk and raw QMP socket under a distinct
service identity before exposing this adapter to an unrestricted agent process.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import string
import threading
import time
import uuid

from .contract import validate
from .state import Store

SIZE = (1440, 900)  # The existing public Computer coordinate contract.
CAPTURE_LIMIT = 5 * 1024 * 1024
TEXT_ACTION_EVENT_LIMIT = 63  # Preserve the existing bounded text-action contract.
KEY_REPORT_SETTLE_S = 1.0
KEY_STROKE_SETTLE_S = 0.05
KEY_CHORD_SETTLE_S = 0.1
INPUT_OPS = frozenset({"click", "type", "key", "scroll"})
KEYS = {
    "Return": ("ret",), "Tab": ("tab",), "Escape": ("esc",),
    "BackSpace": ("backspace",), "Delete": ("delete",),
    "Up": ("up",), "Down": ("down",), "Left": ("left",), "Right": ("right",),
    "Home": ("home",), "End": ("end",), "Page_Up": ("pgup",),
    "Page_Down": ("pgdn",), "ctrl+l": ("ctrl", "l"),
    "ctrl+a": ("ctrl", "a"), "ctrl+c": ("ctrl", "c"),
    "ctrl+v": ("ctrl", "v"), "alt+Tab": ("alt", "tab"),
    "super+Return": ("meta_l", "ret"), "super+Space": ("meta_l", "spc"),
    "super+W": ("meta_l", "w"),
}
PLAIN = "`1234567890-=qwertyuiop[]\\asdfghjkl;'zxcvbnm,./ "
SHIFTED = '~!@#$%^&*()_+QWERTYUIOP{}|ASDFGHJKL:"ZXCVBNM<>? '
PUNCTUATION = dict(zip("`-=[]\\;',./ ", (
    "grave_accent", "minus", "equal", "bracket_left", "bracket_right",
    "backslash", "semicolon", "apostrophe", "comma", "dot", "slash", "spc")))
MODIFIER_CODES = frozenset({"shift", "ctrl", "alt", "meta_l"})
INPUT_KEY_CODES = frozenset(
    string.ascii_lowercase + string.digits) | frozenset(PUNCTUATION.values()) | frozenset(
        code for codes in KEYS.values() for code in codes) | MODIFIER_CODES


def text_keys(value):
    """Validate the entire US-layout string before emitting even its first key."""
    if not isinstance(value, str) or not 1 <= len(value) <= 2000:
        raise ValueError("visual typing requires 1-2000 US-layout characters")
    result = []
    for char in value:
        if char not in PLAIN + SHIFTED:
            raise ValueError("unsupported character; nothing typed (US layout only)")
        shifted = char not in PLAIN
        plain = PLAIN[SHIFTED.index(char)] if shifted else char
        code = plain if plain in string.ascii_lowercase + string.digits else PUNCTUATION[plain]
        result.append((("shift",) if shifted else ()) + (code,))
    return result


def key_event(code, down):
    return {"type": "key", "data": {"down": down, "key": {"type": "qcode", "data": code}}}


def press_events(codes):
    """Build one bounded key-event batch with every intended release included."""
    return ([key_event(code, True) for code in codes]
            + [key_event(code, False) for code in reversed(codes)])


class QmpInputAdapter:
    """One fixed VM, durable non-replayed receipts and explicit operator handover.

``qmp`` must be a pinned, authenticated local transport; ``capture`` must return
only guest PNG bytes normalized to SIZE, never the host desktop. Neither is a
model argument. ``check_vm`` proves the configured VM boundary and capture
geometry; failure means no input. A service/OS isolation layer is still required
before this adapter can be exposed to an unrestricted agent installation.
"""

    def __init__(self, *, root, owner, qmp, capture, check_vm, capture_root=None,
                 capture_mode=0o600):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.capture_root = Path(capture_root) if capture_root else self.root
        self.capture_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if capture_mode & ~0o660:
            raise ValueError("capture mode may not grant access to other users")
        self.capture_mode = capture_mode
        self.owner, self.qmp, self.capture_source, self.check_vm = owner, qmp, capture, check_vm
        self.store = Store(self.root / "jobs.db")
        self.lock = threading.RLock()
        self.input_lock = threading.Lock()
        self.epoch = 0
        self.held = set()
        # Stop first. A QMP disconnect terminates the API process immediately;
        # no validation may run while a possibly resumed guest is still moving.
        # The replacement process repeats this stop before doing anything else.
        self.qmp("stop")
        pending = self._validated_pending()
        self.store.recover()
        self.check_vm()
        self.held.update(pending)
        self._release()

    def _pending(self, codes):
        ordered = tuple(codes)
        self._validate_pending(ordered)
        self.store.pending_keys(ordered)
        self.held = set(ordered)

    @staticmethod
    def _validate_pending(codes):
        if (len(codes) > 2 or any(code not in INPUT_KEY_CODES for code in codes)
                or (len(codes) == 2 and (
                    codes[0] not in MODIFIER_CODES or codes[1] in MODIFIER_CODES))):
            raise ValueError("invalid pending keyboard state")
        return codes

    def _validated_pending(self):
        return self._validate_pending(self.store.pending_keys())

    def _release(self):
        pending = list(self._validated_pending())
        pending.extend(key for key in sorted(self.held) if key not in pending)
        if pending:
            # Keys are stored in press order. Release the target first and then
            # modifiers in reverse order so no bare target remains held.
            self.qmp("input-send-event", {
                "events": [key_event(key, False) for key in reversed(pending)]})
            self.store.pending_keys(())
            self.held.clear()

    def control(self, state, *, human=False):
        if state not in {"human", "agent", "paused", "stopped"}:
            raise ValueError("unknown control state")
        with self.lock:
            if state == "human" and not human:
                raise ValueError("takeover belongs to the operator")
            if self.store.control() == "human" and state == "agent" and not human:
                raise ValueError("only the operator can return control")
            self.epoch += 1
            self.store.control("paused")
            try:
                # Freeze the guest before release or preflight. If either later
                # operation loses QMP and terminates this process, launchd's
                # replacement starts from the same stop-first sequence.
                self.qmp("stop")
                self._release()
                self.check_vm()
                if state in {"human", "agent"}:
                    # Resume is deliberately last: a lost response forces the
                    # replacement process to stop before its first preflight.
                    self.qmp("cont")
            except Exception:
                self.qmp("stop")
                raise
            with self.store.connect() as db:
                db.execute("UPDATE jobs SET state='interrupted',updated=? WHERE state IN ('queued','running')",
                           (time.time(),))
            self.store.control(state)
            return {"control": state}

    def _active(self, epoch, owner="agent"):
        if self.store.control() != owner or self.epoch != epoch:
            raise RuntimeError("operator interrupted input; inspect before retrying")

    def _keys(self, codes, epoch, owner="agent"):
        if len(codes) == 1:
            phases = ((press_events(codes), tuple(codes), ()),)
        else:
            modifiers, target = codes[:-1], codes[-1]
            pressed = []
            phases = []
            for code in modifiers:
                pressed.append(code)
                phases.append(([key_event(code, True)], tuple(pressed), None))
            phases.append((
                [key_event(target, True), key_event(target, False)],
                tuple(pressed + [target]), tuple(pressed)))
            for code in reversed(modifiers):
                pressed.remove(code)
                phases.append(([key_event(code, False)], None, tuple(pressed)))
        for index, (events, before, after) in enumerate(phases):
            with self.lock:
                self._active(epoch, owner)
                # A chord must cross the guest input boundary as observable
                # modifier/key/release phases. One simultaneous QMP frame can be
                # acknowledged yet missed by the guest compositor. Persist each
                # possibly-held key before dispatch so a stopped replacement can
                # release it without replaying the uncertain phase.
                if before is not None:
                    self._pending(before)
                self.qmp("input-send-event", {"events": events})
                if after is not None:
                    self._pending(after)
            if index + 1 < len(phases):
                # Keep takeover immediate between phases.
                time.sleep(KEY_CHORD_SETTLE_S)
        time.sleep(KEY_REPORT_SETTLE_S)
        with self.lock:
            self._active(epoch, owner)

    def _type(self, strokes, epoch, owner="agent"):
        """Submit one text action once as bounded, ordered key reports."""
        if sum(len(press_events(codes)) for codes in strokes) > TEXT_ACTION_EVENT_LIMIT:
            raise RuntimeError("validated keyboard action grew unexpectedly")
        for index, codes in enumerate(strokes):
            with self.lock:
                self._active(epoch, owner)
                # A QMP acknowledgement does not prove that virtio consumed a
                # large descriptor batch. Keep one validated high-level action
                # and never replay it, but bound each report to one complete
                # stroke with all modifier releases included.
                self._pending(codes)
                try:
                    self.qmp("input-send-event", {"events": press_events(codes)})
                except Exception:
                    raise
                else:
                    self._pending(())
            if index + 1 < len(strokes):
                # Do not hold the ownership lock here: an operator takeover
                # must invalidate the epoch before the next report is sent.
                time.sleep(KEY_STROKE_SETTLE_S)
        time.sleep(KEY_REPORT_SETTLE_S)
        with self.lock:
            self._active(epoch, owner)

    def _button(self, button, epoch, position=(), owner="agent"):
        with self.lock:
            self._active(epoch, owner)
            # Keep position, press and release in one bounded batch. If QMP loses
            # the response, service fail-stop removes the session without a
            # second press or a best-effort replay.
            self.qmp("input-send-event", {"events": list(position) + [
                {"type": "btn", "data": {"down": True, "button": button}},
                {"type": "btn", "data": {"down": False, "button": button}},
            ]})

    @staticmethod
    def _input(args, *, human=False):
        if not isinstance(args, dict):
            raise ValueError("desktop input must be an object")
        op = args.get("op")
        allowed = {"op"}
        strokes = None
        if op == "type":
            allowed.add("text")
            strokes = text_keys(args.get("text"))
            report_size = sum(len(press_events(codes)) for codes in strokes)
            if report_size > TEXT_ACTION_EVENT_LIMIT:
                raise ValueError("visual typing exceeds the bounded text-action budget; nothing typed")
            if human and len(args["text"]) > 1024:
                raise ValueError("human input is limited to 1024 characters")
        elif op == "key":
            allowed.add("keys")
            if args.get("keys") not in KEYS:
                raise ValueError("unsupported key combination")
        elif op == "click":
            allowed.update({"x", "y", "button"})
            for name, limit in (("x", SIZE[0] - 1), ("y", SIZE[1] - 1)):
                if type(args.get(name)) is not int or not 0 <= args[name] <= limit:
                    raise ValueError("click lies outside the desktop")
            if args.get("button", 1) not in (1, 2, 3):
                raise ValueError("unsupported mouse button")
        elif op == "scroll":
            allowed.update({"direction", "amount"})
            if args.get("direction") not in {"up", "down"}:
                raise ValueError("unsupported scroll direction")
            if type(args.get("amount", 3)) is not int or not 1 <= args.get("amount", 3) <= 10:
                raise ValueError("scroll amount must be between 1 and 10")
        else:
            raise ValueError("visual desktop supports click, type, key and scroll")
        if set(args) - allowed:
            raise ValueError("unknown desktop input field")
        return op, strokes

    def _perform(self, op, args, strokes, epoch, owner="agent"):
        if op == "type":
            self._type(strokes, epoch, owner)
        elif op == "key":
            self._keys(KEYS[args["keys"]], epoch, owner)
        elif op == "click":
            position = [{"type": "abs", "data": {"axis": axis,
                        "value": round(args[name] * 32767 / (limit - 1))}}
                        for axis, name, limit in (("x", "x", SIZE[0]), ("y", "y", SIZE[1]))]
            self._button({1: "left", 2: "middle", 3: "right"}[args.get("button", 1)],
                         epoch, position, owner)
        else:
            for _ in range(args.get("amount", 3)):
                self._button("wheel-" + args["direction"], epoch, owner=owner)
                time.sleep(0.03)

    def action(self, args):
        op = validate(args)
        if op in {"pause", "resume", "stop"}:
            return self.control({"pause": "paused", "resume": "agent", "stop": "stopped"}[op])
        if op not in INPUT_OPS or args.get("checks"):
            raise ValueError("visual backend supports desktop input only; no exec/browser/file verification")
        input_args = {name: value for name, value in args.items()
                      if name in {"op", "text", "keys", "x", "y", "button", "direction", "amount"}}
        op, strokes = self._input(input_args)
        # One physical input sequence at a time. Handover never waits for this lock.
        if not self.input_lock.acquire(blocking=False):
            raise ValueError("desktop input is busy")
        try:
            with self.lock:
                epoch = self.epoch
                self._active(epoch)
                self.check_vm()
                job, created = self.store.begin(self.owner, args)
                if not created:
                    return {"job": job, "reused": True}
                self.store.finish(job["id"], "running")
            try:
                self._perform(op, input_args, strokes, epoch)
                with self.lock:
                    self._active(epoch)
                    self.store.finish(job["id"], "needs_review", {
                        "verification": "input dispatched; inspect the resulting screen for delivery and task success"})
            except Exception:
                with self.lock:
                    self.store.finish(job["id"], "interrupted", {
                        "verification": "input may be partial; no automatic retry"})
                    # A late worker must not freeze a desktop already handed to
                    # its human owner, or undo a subsequent explicit control change.
                    if self.epoch == epoch:
                        self.store.control("paused")
                        try:
                            self._release()
                        finally:
                            self.qmp("stop")
            return {"job": self.store.get(self.owner, job["id"]), "reused": False}
        finally:
            self.input_lock.release()

    def submit(self, args):
        """Persist a desktop job and return before its physical input is emitted."""
        op = validate(args)
        if op in {"pause", "resume", "stop"}:
            return self.action(args)
        if op not in INPUT_OPS or args.get("checks"):
            raise ValueError("visual desktop supports visual input only")
        input_args = {name: value for name, value in args.items()
                      if name in {"op", "text", "keys", "x", "y", "button", "direction", "amount"}}
        self._input(input_args)
        with self.lock:
            self._active(self.epoch)
            self.check_vm()
            job, created = self.store.begin(self.owner, args)
            if created:
                threading.Thread(target=self._submitted, args=(job["id"], args),
                                 daemon=True).start()
            return {"job": job, "reused": not created}

    def _submitted(self, job_id, args):
        if not self.input_lock.acquire(blocking=False):
            self.store.finish(job_id, "interrupted", {"verification": "desktop input was busy"})
            return
        try:
            input_args = {name: value for name, value in args.items()
                          if name in {"op", "text", "keys", "x", "y", "button", "direction", "amount"}}
            op, strokes = self._input(input_args)
            with self.lock:
                epoch = self.epoch
                self._active(epoch)
                self.check_vm()
                self.store.finish(job_id, "running")
            self._perform(op, input_args, strokes, epoch)
            with self.lock:
                self._active(epoch)
                self.store.finish(job_id, "needs_review", {
                    "verification": "input dispatched; inspect the resulting screen for delivery and task success"})
        except Exception:
            self.store.finish(job_id, "interrupted", {
                "verification": "input may be partial; no automatic retry"})
            with self.lock:
                if self.store.control() == "agent":
                    self.store.control("paused")
                    try:
                        self._release()
                    finally:
                        self.qmp("stop")
        finally:
            self.input_lock.release()

    def human_input(self, args):
        """Accept operator input only while an explicit human takeover is active."""
        op, strokes = self._input(args, human=True)
        if not self.input_lock.acquire(blocking=False):
            raise ValueError("desktop input is busy")
        try:
            with self.lock:
                epoch = self.epoch
                self._active(epoch, "human")
                self.check_vm()
            self._perform(op, args, strokes, epoch, "human")
            return {"input": "dispatched", "control": "human"}
        except Exception:
            with self.lock:
                self.store.control("paused")
                try:
                    self._release()
                finally:
                    self.qmp("stop")
            raise
        finally:
            self.input_lock.release()

    def read(self, args):
        op = validate(args, read=True)
        if op == "status":
            return {"backend": "qmp", "control": self.store.control(),
                    "vm": self.qmp("query-status"), "jobs": self.store.jobs(self.owner),
                    "supported_actions": sorted(INPUT_OPS), "desktop_size": SIZE}
        if op == "job":
            return self.store.get(self.owner, args["job_id"])
        if op != "screenshot":
            raise ValueError("visual backend does not expose guest files or routines")
        with self.lock:
            self.check_vm()
            raw = self.capture_source()
            if (not isinstance(raw, bytes) or len(raw) < 33 or len(raw) > CAPTURE_LIMIT
                    or raw[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
                    or int.from_bytes(raw[16:20], "big") != SIZE[0]
                    or int.from_bytes(raw[20:24], "big") != SIZE[1]):
                raise ValueError("capture source must supply a guest-only 1440x900 PNG")
            if sum(p.stat().st_size for p in self.capture_root.glob("screen-*.png")) + len(raw) > 128 * 1024 * 1024:
                raise ValueError("capture budget reached; existing evidence preserved")
            path = self.capture_root / ("screen-" + uuid.uuid4().hex + ".png")
            with path.open("xb") as stream:
                stream.write(raw)
            path.chmod(self.capture_mode)
            return {"image_path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                    "captured_at": time.time(), "desktop_size": SIZE}

    def runner(self, request):
        if str(request.identity) != self.owner:
            raise ValueError("identity does not own this desktop")
        if request.tool == "computer_status":
            result = self.read(request.args)
        elif request.tool == "computer_run":
            result = self.action(request.args)
        else:
            raise ValueError("unknown desktop tool")
        return json.dumps(result)
