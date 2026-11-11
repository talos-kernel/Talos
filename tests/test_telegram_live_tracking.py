"""Telegram activity communicates measured states without exposing tool arguments."""
from talos.agent_loop import AgentProgress, ProgressStage
from test_telegram_progress_updates import setup_activity
from test_telegram_ux import _tool, _result


def test_live_phase_indicator_changes_only_the_header_and_uses_existing_tick():
    client, activity = setup_activity()
    activity.progress(_tool('run_shell', summary='shell — pytest'))
    initial = client.sent[0][1]
    assert 'Running .' in initial
    assert '<code>run_shell</code>' in initial and 'Running pytest' in initial
    client.now[0] = 5
    activity.tick()
    assert 'Running ..' in client.edited[-1][2]
    assert len(client.sent) == 1
    activity.progress(_result('run_shell', 'done'))
    client.now[0] = 10
    activity.progress(AgentProgress(ProgressStage.THINKING, step=2))
    assert 'Thinking ...' in client.edited[-1][2]
    assert '1 completed · 0 failed or refused' in client.edited[-1][2]
    activity.succeed()


def test_reduced_motion_disables_the_indicator_but_keeps_measured_time(monkeypatch):
    monkeypatch.setenv('TALOS_STATUS_ANIMATION', '0')
    client, activity = setup_activity()
    activity.progress(_tool('read_file', summary='read — notes.md'))
    before = client.sent[0][1].splitlines()[0]
    client.now[0] = 15
    activity.tick()
    assert client.edited[-1][2].splitlines()[0] == before
    assert '15s' in client.edited[-1][2]
    activity.succeed()


def test_approval_parks_live_edits_and_resumes_on_a_real_result():
    client, activity = setup_activity()
    activity.progress(_tool('write_file', summary='write — notes.md'))
    activity.progress(_result('write_file', 'needs_human'))
    before = len(client.edited), len(client.sent)
    client.now[0] = 90
    activity.tick()
    assert (len(client.edited), len(client.sent)) == before
    assert 'Needs your approval' in client.edited[-1][2]
    activity.progress(_result('write_file', 'done'))
    assert '1 completed' in client.edited[-1][2]
    activity.succeed()


def test_waiting_hint_is_based_on_events_and_clears_when_work_returns():
    client, activity = setup_activity()
    activity.progress(_tool('run_shell', summary='shell — pytest'))
    client.now[0] = 30
    activity.tick()
    assert 'Waiting for the next result' in client.edited[-1][2]
    assert '0 completed' in client.edited[-1][2]
    activity.progress(_result('run_shell', 'done'))
    assert 'Waiting for the next result' not in client.edited[-1][2]
    assert '1 completed' in client.edited[-1][2]
    activity.succeed()


def test_code_labels_never_reveal_commands_and_dynamic_html_is_escaped():
    client, activity = setup_activity(name='<b>Example</b>')
    activity.progress(_tool('run_shell', summary='curl --password PRIVATE_CANARY'))
    activity.progress(_tool('read_file', summary='read — <script>alert(1)</script>'))
    rendered = client.edited[-1][2]
    assert 'PRIVATE_CANARY' not in rendered and 'curl' not in rendered
    assert '<script>' not in rendered and '&lt;script&gt;' in rendered
    assert '<code>run_shell</code>' in rendered and '<code>read_file</code>' in rendered
    activity.succeed()


def test_failure_and_frozen_trail_keep_evidence_without_live_indicator():
    client, activity = setup_activity()
    activity.progress(_tool('run_shell', summary='shell — pytest'))
    activity.progress(_result('run_shell', 'error'))
    assert '<b>Running pytest</b>' in client.edited[-1][2]
    assert '<i>Failed</i>' in client.edited[-1][2]
    activity.succeed()
    assert 'Work trail' in client.edited[-1][2]
    assert 'Reply done with issues' in client.sent[-1][1]
    before = len(client.edited)
    client.now[0] = 90
    activity.tick()
    assert len(client.edited) == before


def test_fatal_error_freezes_existing_live_header_as_stopped():
    client, activity = setup_activity()
    activity.progress(_tool('run_shell', summary='shell — pytest'))
    activity.fail('Cancelled.')
    assert 'Stopped' in client.edited[-1][2].splitlines()[0]
    assert 'Running .' not in client.edited[-1][2]
    assert 'failed: Cancelled.' in client.sent[-1][1]
