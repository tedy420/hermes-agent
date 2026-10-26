"""An open view of a messaging chat's conversation goes read-only once the chat moves on.

A Telegram chat's conversation A is live in the authority and open in Desktop (a warm view on the
lane's route). The platform user sends ``/new`` and then a message: the lane publishes conversation
B on the same route. Everything route-keyed (a turn's execution, the agent a Stop reaches) now acts
on B, so A's view may only read, exactly like a cold mount of a row its lane moved past (7ba53f47569).
A ``/resume`` of A on the platform makes A the lane's conversation again and B's view read-only.
Real GatewayRunner, SessionAuthority, SessionStore and SessionDB; the platform commands run through
their own handlers.
"""
import weakref
from pathlib import Path

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.run_runtime import initialize_gateway_runtime
from gateway.runtime_ownership import process_ownership
from gateway.session import SessionSource
from gateway.session_contract import Principal
from gateway.session_ingress_context import register_transport_home
from hermes_state_runtime import RuntimeStoreError

_WRITES = ('session:submit', 'session:control', 'session:approve', 'session:respond')


class _Adapter:
    pass


@pytest.fixture
def served(tmp_path, monkeypatch):
    root = tmp_path / '.hermes'
    root.mkdir(mode=0o700)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(root))
    import hermes_state
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', hermes_state._IMPORT_DEFAULT_DB_PATH)
    config = GatewayConfig()
    config._runtime_profile_homes = (('default', root),)
    runner = GatewayRunner(config)
    process_ownership.reserve([root])
    yield runner, root
    for served_authority in list(getattr(runner, 'session_authorities', []) or []):
        served_authority.db.close()
    process_ownership.release(root)


async def _start(runner, root):
    await initialize_gateway_runtime(runner)
    register_transport_home(runner, None, root)
    adapter = _Adapter()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._is_user_authorized_for_source = lambda *a, **k: True
    from gateway.session_authorities import active_authority
    return runner, active_authority(runner), adapter


def _chat(adapter, chat_id='4242'):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, chat_type='dm', user_id='u1',
                           user_name='probe')
    source._transport_adapter_ref = weakref.ref(adapter)
    return source


def _viewer(authority):
    return Principal('viewer', authority.profile_id, frozenset({'session:read', *_WRITES}), 'socket')


async def _command(runner, source, text):
    event = MessageEvent(text=text, message_type=MessageType.TEXT, source=source, message_id='m-' + text)
    handler = runner._handle_reset_command if text.startswith('/new') else runner._handle_resume_command
    return await handler(event)


def _refused(authority, viewer, ref):
    out = {}
    for capability in _WRITES:
        try:
            authority.authorize(viewer, ref, capability)
            out[capability] = 'accepted'
        except RuntimeStoreError as exc:
            out[capability] = exc.reason
    return out


@pytest.mark.asyncio
async def test_an_open_view_goes_read_only_when_its_chat_moves_to_a_new_conversation(served):
    runner, authority, adapter = await _start(*served)
    viewer = _viewer(authority)
    chat, other_chat = _chat(adapter), _chat(adapter, '777')
    a = authority.register(chat)  # the chat's first message
    other = authority.register(other_chat)
    authority.authorize(viewer, a, 'session:submit')  # Desktop opens A while it is the chat's conversation

    await _command(runner, chat, '/new')
    # Read-only from the /new on: a turn on A would already run in the chat's new conversation.
    assert _refused(authority, viewer, a) == dict.fromkeys(_WRITES, 'session_superseded')
    b = authority.register(chat)  # the chat's next message publishes the new conversation
    assert b.session_id != a.session_id

    assert _refused(authority, viewer, a) == dict.fromkeys(_WRITES, 'session_superseded')
    authority.authorize(viewer, a, 'session:read')
    assert authority.agent(a) is None and authority._runs_own_agent(a) is False
    # The chat's own conversation and another chat's stay writable.
    assert _refused(authority, viewer, b) == dict.fromkeys(_WRITES, 'accepted')
    assert _refused(authority, viewer, other) == dict.fromkeys(_WRITES, 'accepted')


@pytest.mark.asyncio
async def test_resuming_the_old_conversation_on_the_chat_makes_it_writable_again(served):
    runner, authority, adapter = await _start(*served)
    viewer = _viewer(authority)
    chat = _chat(adapter)
    a = authority.register(chat)
    authority.db.set_session_title(a.session_id, 'First chat')
    authority.authorize(viewer, a, 'session:submit')
    await _command(runner, chat, '/new')
    b = authority.register(chat)
    authority.authorize(viewer, b, 'session:submit')  # Desktop also has B open

    await _command(runner, chat, '/resume First chat')
    writable, read_only = dict.fromkeys(_WRITES, 'accepted'), dict.fromkeys(_WRITES, 'session_superseded')
    assert (_refused(authority, viewer, a), _refused(authority, viewer, b)) == (writable, read_only)
    assert authority.register(chat) == a  # the chat's next message lands in A again
    assert (_refused(authority, viewer, a), _refused(authority, viewer, b)) == (writable, read_only)
