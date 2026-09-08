"""A browser reconnect must not redirect an observed target to another tab."""
import pytest

from talos.computer.browser import select_page
from talos.computer.contract import validate
from talos.channel import Principal
from talos.policy import PolicyKernel, ToolRequest, Verdict
from talos.tools import default_manifest

TAB = 'A' * 32
ACTION = {'op': 'browser', 'project': 'fixture', 'key': 'inspect-tab',
          'title': 'Inspect fixture', 'action': 'inspect', 'tab': TAB}


class Context:
    def __init__(self, pages):
        self.pages = pages
        self.detached = 0

    def new_cdp_session(self, page):
        context = self
        class Session:
            def send(self, command):
                assert command == 'Target.getTargetInfo'
                return {'targetInfo': {'targetId': page}}
            def detach(self):
                context.detached += 1
        return Session()


@pytest.mark.parametrize('pages', [[TAB, 'B' * 32], ['B' * 32, TAB]])
def test_reconnected_tab_order_does_not_change_the_selected_target(pages):
    context = Context(pages)
    assert select_page(context, ACTION) == TAB
    assert context.detached == pages.index(TAB) + 1


def test_closed_target_does_not_fall_back_to_a_remaining_tab():
    context = Context(['B' * 32])
    with pytest.raises(ValueError, match='no longer exists'):
        select_page(context, ACTION)
    assert context.detached == 1


@pytest.mark.parametrize('change', [
    {'tab': None}, {'tab': ''}, {'tab': 'A' * 33}, {'tab': True},
    {'tab': 'A' * 32 + '\n'}, {'tab': 'https://example.com'}, {'page': 0},
])
def test_invalid_or_ambiguous_tab_identifiers_are_rejected(change):
    with pytest.raises(ValueError):
        validate(ACTION | change)


def test_stable_tab_target_keeps_computer_permission_and_identity_boundaries(monkeypatch):
    monkeypatch.setenv('TALOS_COMPUTER_SOCKET', '/run/example/control.sock')
    owner = Principal('telegram', '100000001')
    kernel = PolicyKernel(default_manifest(), frozenset({owner}), shell_needs_human=False)
    assert validate(ACTION) == 'browser'
    assert kernel.decide(ToolRequest('computer_run', owner, ACTION)).verdict is Verdict.NEEDS_HUMAN
    assert kernel.decide(ToolRequest('computer_run', Principal('telegram', '200000002'), ACTION)).verdict is Verdict.DENY
