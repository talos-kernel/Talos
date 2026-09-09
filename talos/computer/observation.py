"""Bounded visual follow-up proposals. Execution still belongs to the kernel."""
import json
from pathlib import PurePosixPath
import re

CAPTURE_ROOT = PurePosixPath("/var/lib/talos-computer-captures")


def image_followup(args, result):
    if args.get("op") != "screenshot" or not args.get("question"):
        return None
    try:
        receipt = json.loads(result) if isinstance(result, str) else result
        path = receipt["image_path"]
        if not isinstance(path, str):
            return None
        target = PurePosixPath(path)
        if target.parent != CAPTURE_ROOT or not re.fullmatch(r"screen-[0-9a-f]{32}\.png", target.name):
            return None
    except (TypeError, KeyError, ValueError):
        return None
    # Do not load bytes here. The normal see_image target extractor and capability
    # verifier must run first, including secret/symlink floors and operator consent.
    return "TOOL_CALL: " + json.dumps({
        "tool": "see_image", "args": {"path": path, "question": args["question"]},
    })


class ObservationProgress:
    def __init__(self):
        self.previous = None
        self.repeated = 0

    def record(self, tool, args, result):
        if tool == "computer_run":
            self.previous = None
            self.repeated = 0
            return ""
        if tool != "computer_status" or args.get("op") != "screenshot":
            return ""
        try:
            receipt = json.loads(result) if isinstance(result, str) else result
            digest = receipt.get("sha256", "")
            if not re.fullmatch(r"[a-f0-9]{64}", digest):
                return ""
        except (TypeError, ValueError, AttributeError):
            return ""
        key = (digest, args.get("question", ""))
        self.repeated = self.repeated + 1 if key == self.previous else 1
        self.previous = key
        if self.repeated < 2:
            return ""
        return (
            "[Computer observation: the backend reports unchanged image content for "
            f"{self.repeated} consecutive captures with the same question and no intervening "
            "computer action request. Another identical capture is not progress. "
            "Use the existing visual answer, change the inspection approach, or wait only "
            "for a specific observable transition. Do not replay an uncertain click or submit. "
            "For visual reading, include question in computer_status(screenshot); the next "
            "step reads that exact capture through the normal see_image gate.]"
        )
