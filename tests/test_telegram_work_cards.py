"""Native Telegram work cards use actual projected calls and executor verdicts."""
from xml.etree import ElementTree

import pytest

from talos.agent_loop import AgentProgress, ProgressStage
from talos.telegram import TelegramActivity
from test_telegram_progress_updates import setup_activity
from test_telegram_ux import FakeTelegramClient, _result, _tool


def shell(command='hostname; df -h', **kwargs):
    return AgentProgress(ProgressStage.TOOL, tool='run_shell', preview=command, **kwargs)


def test_native_code_block_names_the_command_and_only_result_proves_completion():
    client, activity = setup_activity(min_edit_interval=0)
    activity.progress(shell())
    text = client.sent[-1][1]
    assert '<b>Terminal</b>' in text and 'In progress' in text
    assert '<pre><code class="language-bash">hostname;\ndf -h</code></pre>' in text
    assert 'Done' not in text
    assert client.sent[-1][2]['parse_mode'] == 'HTML'
    client.now[0] = 3
    activity.progress(_result('run_shell', 'done'))
    assert '<i>Done · 3s</i>' in client.edited[-1][2]
    activity.succeed()
    assert 'Work trail' in client.edited[-1][2]
    assert 'hostname' in client.edited[-1][2]
    assert sum('Reply done' in text for _, text, _ in client.sent) == 1


@pytest.mark.parametrize('status, expected', [
    ('denied', 'Not executed · refused'), ('binding_changed', 'Not executed · changed'),
    ('needs_human', 'Needs your approval'), ('error', 'Failed'),
    ('verify_failed', 'Verification failed'), ('unknown', 'Result unavailable'),
])
def test_every_verdict_has_an_honest_separate_state(status, expected):
    client, activity = setup_activity(min_edit_interval=0)
    activity.progress(shell())
    activity.progress(_result('run_shell', status))
    text = client.edited[-1][2]
    assert '<i>' + expected + '</i>' in text
    assert '<i>Done' not in text
    activity.succeed(receipt=status != 'needs_human')
    if status == 'needs_human':
        assert not any('Reply done' in text for _, text, _ in client.sent)


def test_missing_result_never_becomes_a_successful_tool():
    client, activity = setup_activity()
    activity.progress(shell())
    activity.succeed()
    assert 'No result recorded' in client.edited[-1][2]
    assert '<i>Done' not in client.edited[-1][2]

    class FailingClient(FakeTelegramClient):
        def send_message(self, chat_id, text, **kwargs):
            if text.startswith('❌'):
                raise OSError('offline')
            return super().send_message(chat_id, text, **kwargs)

    client, activity = setup_activity(FailingClient())
    activity.progress(shell())
    activity.fail('<script>&error')
    text = client.edited[-1][2]
    assert '&lt;script&gt;&amp;error' in text
    ElementTree.fromstring('<root>' + text + '</root>')


def test_channel_reprojects_untrusted_preview_and_validates_remote_alias():
    client, activity = setup_activity(min_edit_interval=0)
    activity.progress(AgentProgress(ProgressStage.TOOL, tool='remote_exec',
        preview='curl -H "Authorization: Bearer ordinaryword" https://example.test',
        target_label='token=ordinaryword'))
    text = client.sent[-1][1]
    assert '<b>Remote terminal</b>' in text
    assert 'curl [arguments hidden]' in text and 'ordinaryword' not in text
    activity.progress(AgentProgress(ProgressStage.TOOL, tool='remote_exec',
        preview='hostname', target_label='demo-host'))
    assert '<code>demo-host</code>' in client.edited[-1][2]
    activity.succeed()


def test_file_and_skill_names_remain_readable_and_html_is_escaped():
    client, activity = setup_activity(min_edit_interval=0, name='<Talos>')
    activity.progress(_tool('skill_view', summary='skill — evidence-hygiene'))
    activity.progress(_result('skill_view', 'done'))
    activity.progress(_tool('read_file', summary='read — <notes>.md'))
    text = client.edited[-1][2]
    assert '<code>evidence-hygiene</code>' in text
    assert '<code>&lt;notes&gt;.md</code>' in text
    assert '&lt;Talos&gt;' in text
    ElementTree.fromstring('<root>' + text + '</root>')
    activity.succeed()


def test_long_trail_drops_whole_cards_and_keeps_valid_bounded_html():
    client, activity = setup_activity(min_edit_interval=0, max_lines=100)
    for _ in range(30):
        activity.progress(shell('; '.join(['systemctl status demo.service'] * 6)))
        activity.progress(_result('run_shell', 'done'))
    text = client.edited[-1][2]
    assert len(text.encode('utf-16-le')) // 2 <= 3800
    assert 'earlier events' in text
    ElementTree.fromstring('<root>' + text + '</root>')
    assert text.count('<pre>') == text.count('</pre>')
    activity.succeed()


def test_geometric_activity_keeps_its_plaintext_contract():
    client = FakeTelegramClient()
    activity = TelegramActivity(client, 42, heartbeat_s=0)
    activity.progress(shell())
    assert 'hostname' not in client.sent[-1][1]
    assert '<pre>' not in client.sent[-1][1]
    assert 'parse_mode' not in client.sent[-1][2]
    activity.succeed()
