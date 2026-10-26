"""Local filesystem handoff contract; no model calls or real profile writes."""
import importlib.util
import json
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / 'plugins' / 'handoff-context' / '__init__.py'


def load_plugin():
    assert PLUGIN.exists(), 'opt-in handoff reader is not implemented'
    spec = importlib.util.spec_from_file_location('handoff_context_test', PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_startup_reads_version_and_records_only_byte_verification(tmp_path):
    module = load_plugin()
    source = tmp_path / 'handoff.md'
    source.write_text('## Context\nA reported result is not independently verified.', encoding='utf-8')
    reader = module.Reader(source, tmp_path / 'state')
    receipt = reader.poll('startup')
    assert receipt['result'] == 'loaded'
    assert receipt['action'] == 'handoff.read'
    assert receipt['trigger'] == 'startup'
    assert receipt['evidence']['sha256'] == module.hashlib.sha256(source.read_bytes()).hexdigest()
    assert receipt['evidence']['verification_scope'] == 'source_bytes_only'
    assert receipt['reference'] == 'plugin:handoff-context'
    assert receipt['at'].endswith('+00:00')
    assert 'reported result' not in json.dumps(receipt)
    assert 'reported result' in reader.context('private-session')
    assert reader.context('private-session') is None
    assert 'reported result' in reader.context('another-session')


def test_hourly_timer_refreshes_and_suppresses_unchanged(tmp_path):
    module = load_plugin()
    source = tmp_path / 'handoff.md'
    source.write_text('first', encoding='utf-8')
    reader = module.Reader(source, tmp_path / 'state')
    reader.poll('startup')
    assert 'first' in reader.context('session')
    intervals = []

    class Clock:
        calls = 0

        def wait(self, seconds):
            intervals.append(seconds)
            self.calls += 1
            if self.calls == 1:
                return False  # no changes at the first hourly tick
            if self.calls == 2:
                source.write_text('second', encoding='utf-8')
                return False
            return True

    reader.run(Clock())
    assert intervals == [3600, 3600, 3600]
    assert 'second' in reader.context('session')
    assert reader.context('session') is None
    import sqlite3
    with sqlite3.connect(tmp_path / 'state' / 'receipts.sqlite3') as db:
        receipts = [json.loads(row[0]) for row in db.execute('SELECT body FROM receipts ORDER BY id')]
    assert [r['result'] for r in receipts] == ['loaded', 'unchanged', 'loaded']
    assert [r['trigger'] for r in receipts] == ['startup', 'hourly', 'hourly']


def test_inaccessible_source_is_nonfatal_and_never_serves_stale_context(tmp_path):
    module = load_plugin()
    source = tmp_path / 'handoff.md'
    reader = module.Reader(source, tmp_path / 'state')
    receipt = reader.poll('startup')
    assert receipt['result'] == 'unavailable'
    assert 'sha256' not in receipt['evidence']
    assert reader.context('session') is None
    source.write_text('recoverable context', encoding='utf-8')
    assert reader.poll('hourly')['result'] == 'loaded'
    assert reader.context('session')
    source.unlink()
    assert reader.poll('hourly')['result'] == 'unavailable'
    assert reader.context('other-session') is None
    source.write_text('recoverable context', encoding='utf-8')
    assert reader.poll('hourly')['result'] == 'unchanged'
    assert reader.context('other-session')


def test_register_reads_at_start_and_delivers_only_to_allowed_session(tmp_path, monkeypatch):
    import hermes_constants
    import threading
    module = load_plugin()
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: tmp_path)
    source = tmp_path / 'handoff.md'
    source.write_text('private context', encoding='utf-8')
    hooks = {}
    threads = []
    cleanups = []

    class Thread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            threads.append(self)

        def start(self):
            self.started = True

    monkeypatch.setattr(threading, 'Thread', Thread)

    class Ctx:
        def get_config(self, key, default=None):
            return {'source': 'handoff.md', 'allowed_session_ids': ['private']}.get(key, default)

        def register_hook(self, name, callback):
            hooks[name] = callback

        def on_unload(self, callback):
            cleanups.append(callback)

    module.register(Ctx())
    assert threads[0].started and threads[0].kwargs['daemon']
    assert len(cleanups) == 1
    cleanups[0]()
    assert threads[0].kwargs['args'][0].is_set()
    assert hooks['pre_llm_call'](session_id='public') is None
    assert hooks['pre_llm_call'](session_id='') is None
    context = hooks['pre_llm_call'](session_id='private')
    assert 'private context' in context['context']
    assert 'not authorization' in context['context']
    assert hooks['pre_llm_call'](session_id='private') is None
    assert hooks['pre_llm_call'](session_id='public', parent_session_id='private') is None


def test_non_documents_and_oversized_sources_are_rejected_without_context(tmp_path):
    module = load_plugin()
    for name, raw in [('handoff.md', b'x' * (1024 * 1024 + 1)),
                      ('auth.json', b'{"token":"do-not-expose"}'),
                      ('bad.md', b'\xff\xfe')]:
        source = tmp_path / name
        source.write_bytes(raw)
        reader = module.Reader(source, tmp_path / 'state')
        receipt = reader.poll('startup')
        assert receipt['result'] == 'unavailable'
        assert reader.context('s') is None
        assert 'sha256' not in receipt['evidence']
        assert 'do-not-expose' not in json.dumps(receipt)


def test_receipt_store_failure_is_nonfatal_and_withholds_context(tmp_path):
    module = load_plugin()
    source = tmp_path / 'handoff.md'
    source.write_text('context', encoding='utf-8')
    state = tmp_path / 'not-a-directory'
    state.write_text('occupied', encoding='utf-8')
    reader = module.Reader(source, state)
    assert reader.poll('startup')['result'] == 'receipt_store_unavailable'
    assert reader.context('s') is None


def test_restart_deduplicates_read_but_delivers_to_new_conversation(tmp_path):
    module = load_plugin()
    source = tmp_path / 'handoff.md'
    source.write_text('context', encoding='utf-8')
    assert module.Reader(source, tmp_path / 'state').poll('startup')['result'] == 'loaded'
    restarted = module.Reader(source, tmp_path / 'state')
    assert restarted.poll('startup')['result'] == 'unchanged'
    assert restarted.context('new-conversation')


def test_real_plugin_loader_reads_private_profile_and_unloads(tmp_path, monkeypatch):
    import sqlite3
    import yaml
    from hermes_cli.plugins import PluginManager, PluginManifest
    assert (PLUGIN.parent / 'plugin.yaml').is_file(), 'plugin is not discoverable'
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    (tmp_path / 'handoff.md').write_text('loader context', encoding='utf-8')
    (tmp_path / 'config.yaml').write_text(yaml.safe_dump({
        'plugins': {'enabled': ['handoff-context'], 'entries': {
            'handoff-context': {'settings': {
                'source': 'handoff.md', 'allowed_session_ids': ['only-private']
            }}
        }}
    }), encoding='utf-8')
    manager = PluginManager(scope_key=str(tmp_path))
    manifest = PluginManifest(name='handoff-context', version='1.0.0',
                              source='bundled', path=str(PLUGIN.parent))
    try:
        manager._load_plugin(manifest)
        loaded = manager._plugins['handoff-context']
        assert not loaded.error
        assert manager.invoke_hook('pre_llm_call', session_id='not-private') == []
        result = manager.invoke_hook('pre_llm_call', session_id='only-private')
        assert 'loader context' in result[0]['context']
        assert manager.invoke_hook('pre_llm_call', session_id='only-private') == []
        db_path = tmp_path / 'state/plugins/handoff-context/receipts.sqlite3'
        with sqlite3.connect(db_path) as db:
            receipt = json.loads(db.execute('SELECT body FROM receipts').fetchone()[0])
        assert receipt['trigger'] == 'startup' and receipt['result'] == 'loaded'
    finally:
        manager.unload('handoff-context')
    assert manager.invoke_hook('pre_llm_call', session_id='only-private') == []
