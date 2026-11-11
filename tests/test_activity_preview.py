"""Display projections preserve useful evidence and omit opaque argument payloads."""
import pytest

from talos.activity_preview import HIDDEN, MAX_PREVIEW, command_preview, safe_label


@pytest.mark.parametrize('command', [
    'hostname', "date '+%Y-%m-%d %H:%M:%S %Z'", 'df -h', 'free -h',
    'systemctl --user show demo.service -p ActiveState,MainPID',
    'systemctl status demo.service --no-pager', 'docker stats --no-stream',
    'git status --short', 'python3 -m pytest tests/test_agent_loop.py -q',
    '/usr/bin/uptime', 'ps -eo pid,comm,rss', 'cat /etc/os-release',
])
def test_known_diagnostic_arguments_remain_visible(command):
    assert command_preview(command) == command


@pytest.mark.parametrize('command', [
    'curl https://example.test/ordinaryword',
    'curl -H "Authorization: Bearer ordinaryword" https://example.test',
    'curl --cookie ordinaryword https://example.test',
    'curl https://user:ordinaryword@example.test/?q=ordinaryword',
    'python3 -c "print(\'ordinaryword\')"',
    'bash -c "echo ordinaryword"', 'qmd search ordinaryword',
    'env VALUE=ordinaryword hostname', 'VALUE=ordinaryword hostname',
    'ssh ordinaryword@host hostname', 'mysql -pordinaryword',
    'systemctl set-environment VALUE=ordinaryword',
    'systemctl show demo.service -p Environment,ordinaryword',
    'systemctl status --password ordinaryword',
    'cat /private/ordinaryword', 'pytest -k ordinaryword',
    '/private/ordinaryword/hostname', 'ordinaryword --version',
    'hostname $(echo ordinaryword)', 'hostname `echo ordinaryword`',
    'hostname; cat <<END\nordinaryword\nEND', 'hostname >ordinaryword',
    'hostname \\ordinaryword', 'hostname\x1b[31mordinaryword',
    'hostname\u202eordinaryword', 'hostname\x00ordinaryword',
])
def test_unknown_arguments_and_shell_bodies_are_never_disclosed(command):
    result = command_preview(command)
    assert 'ordinaryword' not in result
    assert HIDDEN in result


@pytest.mark.parametrize('command', ['hostname &&', 'hostname |', '; hostname',
                                      'hostname & df -h', 'hostname ;; df -h',
                                      'hostname; && df -h', 'hostname "unclosed'])
def test_malformed_structure_is_not_presented_as_a_complete_command(command):
    assert command_preview(command) == 'command ' + HIDDEN


def test_groups_are_readable_and_projection_is_idempotent():
    command = 'hostname; date; df -h && free -h\ndocker ps'
    projected = command_preview(command)
    assert projected == 'hostname;\ndate;\ndf -h &&\nfree -h;\ndocker ps'
    assert command_preview(projected) == projected


def test_quoted_operator_is_an_argument_not_an_extra_command():
    assert command_preview("systemctl status ';'") == 'systemctl ' + HIDDEN


def test_bounded_groups_are_explicitly_shortened_and_stable():
    projected = command_preview('; '.join(['hostname'] * 50))
    assert len(projected) <= MAX_PREVIEW
    assert projected.count('hostname') == 6
    assert projected.endswith('# … preview shortened')
    assert command_preview(projected) == projected
    assert command_preview('hostname ' + 'x' * 9000) == 'command ' + HIDDEN
    oversized = 'pytest ' + ' '.join(['tests/test_long_filename_for_projection.py'] * 8)
    assert command_preview(oversized) == 'command ' + HIDDEN + '\n# … preview shortened'


@pytest.mark.parametrize('value', [None, {}, 4, ''])
def test_non_command_values_are_empty(value):
    assert command_preview(value) == ''


def test_target_labels_are_identifiers_not_paths_or_credentials():
    assert safe_label('demo-host') == 'demo-host'
    for value in ['https://example.test', '/home/operator', 'token=canary',
                  'sk-canary', '<b>demo</b>', 'x' * 65, None]:
        assert safe_label(value) == ''
