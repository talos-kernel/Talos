"""The live harness must not require a Telegram token or optional Hermes checkout."""
from pathlib import Path
import subprocess
import sys


def test_claude_harness_has_no_live_channel_or_optional_catalog_requirement(tmp_path):
    # Import in a child: e2e deliberately disables periodic self-review for its run.
    code = '''
import sys
from pathlib import Path
from types import SimpleNamespace
import e2e
config = SimpleNamespace(hermes_provider_catalog=Path('absent'), hermes_models=Path('absent'),
    hermes_catalog_configured=False, model_provider='claude-cli', model_name='claude-fable-5-1',
    eventlog_db=Path(sys.argv[1])/'events.db', claude_bin='test-cli', reasoner_timeout_s=10)
def load_config(*, require_channel=True):
    assert require_channel is False
    return config
class Loader:
    def __init__(self, *args): pass
    def load_if_present(self): return None
    def load(self): raise ValueError('explicit catalog missing')
e2e.load_config = load_config
e2e.HermesCatalogLoader = Loader
e2e.ClaudeCliReasoner = lambda binary, timeout, meter, *, model: (binary, model)
assert e2e.production_reasoner(None) == ('test-cli', 'claude-fable-5-1')
config.hermes_catalog_configured = True
try:
    e2e.production_reasoner(None)
except ValueError as error:
    assert str(error) == 'explicit catalog missing'
else:
    raise AssertionError('explicit operator configuration must not be silently ignored')
'''
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path)],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
