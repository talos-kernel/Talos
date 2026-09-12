"""Live catalogues must never change the meaning of existing Telegram buttons."""
from pathlib import Path
import threading

from talos.channel import Principal
from talos.eventlog import EventLog
from talos.provider import ModelPicker, ModelRouter, ModelSelection, Provider, ProviderRegistry

OWNER = Principal('telegram', '7')
CHAT = 'telegram:7'

class Reasoner:
    def __init__(self, selection): self.selection = selection
    def reason(self, prompt): return self.selection.model

def reg(*providers):
    return ProviderRegistry(Provider(slug, slug, tuple(names)) for slug, names in providers)

def build(tmp_path, initial, refresh):
    log = EventLog(tmp_path / 'events.db')
    selection = ModelSelection(initial.providers[0].slug, initial.providers[0].models[0])
    router = ModelRouter(initial, selection, Reasoner, log)
    picker = ModelPicker(initial, router, refresh_registry=refresh)
    return picker, router, log

def button(ui, suffix):
    return next(b.data for row in ui.keyboard for b in row if b.data.endswith(suffix))

def handle(picker, data):
    return picker.handle(data, principal=OWNER, conversation=CHAT)

def open_picker(picker):
    return picker.open(principal=OWNER, conversation=CHAT)

def test_new_menu_and_typed_selection_discover_new_models_without_restart(tmp_path):
    initial = reg(('oauth', ['old']))
    current = [initial]
    picker, router, log = build(tmp_path, initial, lambda: current[0])
    open_picker(picker)
    current[0] = reg(('oauth', ['new', 'old']))
    top = open_picker(picker)
    page = handle(picker, button(top, ':p:0'))
    assert page.keyboard[0][0].label == 'new'
    assert router.current == ModelSelection('oauth', 'old')
    assert not log.recent(10, ('model.selected',))
    result = handle(picker, button(page, ':m:0'))
    assert 'switched' in result.text
    assert router.reason('proof') == 'new'
    current[0] = reg(('oauth', ['later', 'old', 'new']))
    assert 'switched' in picker.select_typed('oauth later', principal=OWNER).text
    assert router.current == ModelSelection('oauth', 'later')

def test_old_provider_and_model_buttons_keep_exact_meaning_after_reordering(tmp_path):
    first = reg(('alpha', ['a1', 'a2']), ('beta', ['b1', 'b2']))
    current = [first]
    picker, router, _ = build(tmp_path, first, lambda: current[0])
    old_top = open_picker(picker)
    old_page = handle(picker, button(open_picker(picker), ':p:0'))
    current[0] = reg(('beta', ['b2', 'b1']), ('alpha', ['a2', 'a1']))
    open_picker(picker)
    page = handle(picker, button(old_top, ':p:0'))
    assert page.keyboard[0][1].label == 'a2'
    result = handle(picker, button(old_page, ':m:1'))
    assert 'switched' in result.text
    assert router.current == ModelSelection('alpha', 'a2')

def test_removed_old_button_fails_without_selecting_replacement(tmp_path):
    first = reg(('oauth', ['active', 'removed']))
    current = [first]
    picker, router, log = build(tmp_path, first, lambda: current[0])
    page = handle(picker, button(open_picker(picker), ':p:0'))
    current[0] = reg(('oauth', ['active', 'replacement']))
    open_picker(picker)
    message = handle(picker, button(page, ':m:1'))
    assert 'unchanged' in message.text
    assert router.current == ModelSelection('oauth', 'active')
    assert not log.recent(10, ('model.selected',))

def test_refresh_failure_keeps_last_catalogue_and_current_reasoner(tmp_path):
    initial = reg(('oauth', ['active', 'other']))
    def broken(): raise OSError('private provider diagnostic')
    picker, router, _ = build(tmp_path, initial, broken)
    top = open_picker(picker)
    assert 'private' not in top.text
    assert picker.registry is initial
    assert router.reason('proof') == 'active'
    page = handle(picker, button(top, ':p:0'))
    assert 'switched' in handle(picker, button(page, ':m:1')).text

def test_refresh_does_not_hold_picker_lock_during_network_io(tmp_path):
    initial = reg(('oauth', ['active', 'other']))
    block = [False]; started = threading.Event(); release = threading.Event()
    def load():
        if block[0]:
            started.set()
            assert release.wait(3)
        return initial
    picker, _, _ = build(tmp_path, initial, load)
    top = open_picker(picker)
    block[0] = True
    thread = threading.Thread(target=lambda: open_picker(picker))
    thread.start()
    try:
        assert started.wait(1)
        page = handle(picker, button(top, ':p:0'))
        assert 'Select a model' in page.text
    finally:
        release.set(); thread.join(3)
    assert not thread.is_alive()

def test_refreshed_menu_preserves_sender_and_chat_checks(tmp_path):
    initial = reg(('oauth', ['active']))
    changed = reg(('oauth', ['active', 'new']))
    picker, router, _ = build(tmp_path, initial, lambda: changed)
    top = open_picker(picker)
    data = button(top, ':p:0')
    assert 'invalid' in picker.handle(data, principal=Principal('telegram','8'), conversation=CHAT).text
    assert 'invalid' in picker.handle(data, principal=OWNER, conversation='telegram:8').text
    assert router.current == ModelSelection('oauth', 'active')
