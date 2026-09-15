"""Recorded failure shapes, replayed with fake HTTP and real kernel execution."""
import json
from types import SimpleNamespace
import pytest
from talos.api_reasoner import ApiReasoner, ReasonerFailure
from talos.credentials import CredentialStore, Route
from talos.fallback import FallbackReasoner
from talos.provider import ModelSelection
from talos.agent_loop import run_agent, AgentStatus, announces_next_step
from talos.channel import ChannelRegistry, Trust
from talos.usage import UsageMeter
from tests.test_api_reasoner import FakeHttp, FakeResponse, sse
from tests.test_agent_loop import _executor, OWNER

class Log:
    def __init__(self): self.events = []
    def append(self, event): self.events.append(event)

class Limited:
    current = ModelSelection("claude-cli", "test")
    def reason(self, *args, **kwargs):
        raise ReasonerFailure("test limit", kind="rate_limited")

def api(lines, meter=None):
    result = ApiReasoner(
        "ollama", "test", CredentialStore({"ollama": Route("ollama", "", "https://proxy.example/v1")}),
        timeout_s=5, http=FakeHttp(FakeResponse(lines)), meter=meter, worker="",
    )
    result._compose = lambda prompt: ("synthetic", prompt)
    return result

def text_api(text):
    return api([sse({"choices": [{"delta": {"content": text}}]}), "data: [DONE]"])

@pytest.mark.parametrize("lines", [
    ["data: [DONE]"],
    [sse({"choices": [{"delta": {"reasoning_content": "private thinking"}}]}), "data: [DONE]"],
])
def test_empty_sse_is_typed_not_success(lines):
    meter = UsageMeter()
    with pytest.raises(ReasonerFailure) as caught:
        api(lines, meter).reason_strict("test")
    assert caught.value.kind == "empty_response"
    assert "private" not in str(caught.value)
    assert meter.snapshot().last.ok is False

@pytest.mark.parametrize("error,kind", [
    ({"type": "overloaded_error", "message": "private prompt"}, "overloaded"),
    ({"code": "rate_limit_exceeded", "message": "private prompt"}, "rate_limited"),
    ({"status": 401, "message": "private prompt"}, "key_rejected"),
    ({"type": "unrecognized", "message": "private prompt"}, "http_failed"),
])
def test_stream_error_classification_never_echoes_prompt(error, kind):
    with pytest.raises(ReasonerFailure) as caught:
        api([sse({"error": error})]).reason_strict("test")
    assert caught.value.kind == kind
    assert "private" not in str(caught.value)
    assert "private" not in caught.value.note

def test_empty_fallback_advances_and_never_replays_a_completed_write(tmp_path):
    executor = _executor(tmp_path)
    output = tmp_path / "receipt.txt"
    requests = []
    first = ModelSelection("ollama", "first")
    second = ModelSelection("ollama", "second")
    replies = iter([
        text_api('TOOL_CALL: ' + json.dumps({"tool":"write_file","args":{"path":str(output),"content":"one write"}})),
        api(["data: [DONE]"]),
    ])
    def build(selection):
        requests.append(selection.model)
        return next(replies) if selection == first else text_api("Done; the file is written.")
    fallback = FallbackReasoner(Limited(), (first, second), build, executor.log)
    result = run_agent(lambda history: fallback.reason_strict(str(history)), executor, OWNER, "replay")
    assert result.status is AgentStatus.ANSWERED
    assert output.read_text() == "one write"
    assert requests == ["first", "first", "second"]
    receipts = [row for row in executor.log.by_run("replay") if row["type"] == "exec.result"]
    assert len(receipts) == 1 and receipts[0]["payload"]["status"] == "done"
    hops = [row["payload"] for row in executor.log.recent(50, ("model.fallback.runtime",))]
    assert sum(row["outcome"] == "failed" for row in hops) == 1

def test_exhausted_chain_is_a_failure_for_the_conductor():
    log = Log()
    fallback = FallbackReasoner(Limited(), (ModelSelection("ollama", "empty"),),
                                lambda _: api(["data: [DONE]"]), log)
    with pytest.raises(ReasonerFailure, match="Empty answer"):
        fallback.reason_strict("test")
    assert [event.payload["outcome"] for event in log.events] == ["failed"]

def test_plaintext_empty_adapter_is_checked_before_fallback_prefix():
    log = Log()
    fallback = FallbackReasoner(Limited(), (ModelSelection("ollama", "legacy"),),
                                lambda _: SimpleNamespace(reason=lambda p: "(Empty answer.)"), log)
    with pytest.raises(ReasonerFailure):
        fallback.reason_strict("test")
    assert log.events[-1].payload["outcome"] == "failed"

def test_readme_announcement_continues_with_one_gated_read(tmp_path):
    executor = _executor(tmp_path)
    source = tmp_path / "README.md"
    source.write_text("verified content")
    replies = iter([
        "(Fallback: ollama/test — Grund: limit)\nDie Seite ist zu gross — ich hole die rohe README.",
        'TOOL_CALL: '+json.dumps({"tool":"read_file","args":{"path":str(source)}}),
        "The README confirms verified content.",
    ])
    result = run_agent(lambda _: next(replies), executor, OWNER, "readme")
    assert result.status is AgentStatus.ANSWERED
    assert result.text == "The README confirms verified content."
    assert len([r for r in executor.log.by_run("readme") if r["type"] == "exec.result"]) == 1

def test_repeated_announcement_stops_with_explicit_unfinished_result(tmp_path):
    result = run_agent(lambda _: "Ich hole jetzt die README.", _executor(tmp_path), OWNER, "bounded")
    assert result.status is AgentStatus.STEP_LIMIT and result.steps == 2
    assert "Task unfinished" in result.text

@pytest.mark.parametrize("answer", [
    "Soll ich die README holen?", "Wenn du willst, prüfe ich das.", "I could read it tomorrow.",
    "Die README ist gelesen; Ergebnis: bestanden.", "Er sagte: ich prüfe das.",
])
def test_questions_offers_and_results_are_not_auto_continued(answer):
    assert not announces_next_step(answer)

def test_stop_during_announcement_repair_runs_nothing(tmp_path):
    stopped = [False]
    def propose(_):
        stopped[0] = True
        return "Ich hole jetzt die README."
    executor = _executor(tmp_path)
    result = run_agent(propose, executor, OWNER, "stop", should_stop=lambda: stopped[0])
    assert "Stopped" in result.text
    assert not [r for r in executor.log.by_run("stop") if r["type"] == "exec.result"]

def test_poll_backoff_is_bounded_and_resets_without_holding_healthy_commands():
    now = [0.0]; attempts = []; sleeps = []; errors = []
    class Broken:
        name = "telegram"; trust = Trust.FULL
        broken = True
        def poll(self):
            attempts.append(now[0])
            if self.broken: raise RuntimeError("synthetic 502")
            return []
    class Healthy:
        name = "test"; trust = Trust.FULL
        pending = []
        def poll(self):
            result, self.pending = self.pending, []
            return result
    def sleep(seconds):
        assert 0 < seconds <= 1
        sleeps.append(seconds); now[0] += seconds
    broken, healthy = Broken(), Healthy()
    registry = ChannelRegistry((broken,healthy), on_error=lambda *a: errors.append(a),
                               clock=lambda: now[0], sleep=sleep)
    while now[0] < 100: registry.poll_all()
    assert attempts[:6] == [0,1,3,7,15,31]
    assert max(b-a for a,b in zip(attempts,attempts[1:])) == 30
    healthy.pending = ["command"]
    before = now[0]
    assert registry.poll_all() == ["command"] and now[0] == before
    broken.broken = False; now[0] += 31
    registry.poll_all()
    broken.broken = True
    registry.poll_all()
    assert now[0] == before + 32  # successful polling reset delay to one second

def test_exhausted_fallback_finishes_worker_as_failed_not_answered(tmp_path):
    import threading
    from talos.worker import Worker
    from tests.test_conductor import _build, msg
    fallback = FallbackReasoner(Limited(), (ModelSelection("ollama", "empty"),),
                                lambda _: api(["data: [DONE]"]), Log())
    conductor, sent = _build(tmp_path, fallback)
    worker = Worker(conductor.handle)
    update = msg(1, OWNER, "Read the requested source.")
    ended = threading.Event()
    states = []
    def watch(state):
        states.append(state)
        if state in {"ended", "failed", "cancelled"}:
            ended.set()
    worker.start()
    try:
        assert worker.submit(update)
        worker.watch(update, watch)
        assert ended.wait(3)
        assert states[-1] == "failed"
        assert not worker.busy() and worker.pending() == 0
        assert sent and "Empty answer" in sent[-1][1]
        assert not [r for r in conductor.log.recent(30) if r["type"] == "reason.done"]
    finally:
        worker.stop()

def test_oversized_fetch_has_a_smaller_read_route_without_permission_changes():
    from talos.recovery import advice
    from talos.executor import Status
    failure = SimpleNamespace(status=Status.ERROR, detail="response exceeds 262144 bytes", result=None)
    assert "raw README" in advice("web_fetch", failure, 0)
    assert not advice("web_fetch", failure, 2)
    failure.status = Status.DENIED
    assert not advice("web_fetch", failure, 0)
