"""Persistent island Telegram state: token, chats, link codes, queue."""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.svitgrid.telegram.state import TelegramState, parse_token

TOKEN = "123456:ABCdefGHIjklMNOpqrSTUvwxYZ0123456789"
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


@pytest.fixture
async def state(hass):
    s = TelegramState(hass)
    await s.load()
    await s.set_bot(TOKEN, "my_bot")
    return s


async def _flush(hass, state):
    await state.flush()


async def test_code_is_single_use(state):
    code = await state.issue_code("dev1", "uk", NOW)
    assert len(code) <= 64
    chat = await state.claim_code(code, 42, "ivan", NOW)
    assert chat is not None and chat.device_id == "dev1" and chat.chat_id == 42
    assert chat.enabled and chat.language == "uk"
    assert chat.linked_at == NOW.isoformat()
    assert await state.claim_code(code, 43, None, NOW) is None
    assert state.chat_for("dev1").chat_id == 42


async def test_code_expires_after_10_minutes(state):
    code = await state.issue_code("dev1", "uk", NOW)
    assert await state.claim_code(code, 42, None, NOW + timedelta(minutes=10, seconds=1)) is None
    assert state.chat_for("dev1") is None
    # The expired code is gone, so it cannot be claimed later either.
    assert await state.claim_code(code, 42, None, NOW) is None
    ok = await state.issue_code("dev1", "uk", NOW)
    assert await state.claim_code(ok, 42, None, NOW + timedelta(minutes=9)) is not None


async def test_relink_same_device_replaces_chat(state):
    c1 = await state.issue_code("dev1", "uk", NOW)
    await state.claim_code(c1, 42, "a", NOW)
    c2 = await state.issue_code("dev1", "en", NOW)
    await state.claim_code(c2, 99, "b", NOW)
    assert [c.chat_id for c in state.chats()] == [99]
    assert state.chat_for("dev1").language == "en"


async def test_issue_code_replaces_pending_code_of_device(state):
    old = await state.issue_code("dev1", "uk", NOW)
    new = await state.issue_code("dev1", "uk", NOW)
    assert await state.claim_code(old, 1, None, NOW) is None
    assert await state.claim_code(new, 1, None, NOW) is not None


async def test_same_chat_moves_to_claiming_device(state):
    await state.claim_code(await state.issue_code("dev1", "uk", NOW), 42, None, NOW)
    await state.claim_code(await state.issue_code("dev2", "uk", NOW), 42, None, NOW)
    assert state.chat_for("dev1") is None
    assert state.chat_for("dev2").chat_id == 42


async def test_has_pending_codes_and_pruning(state):
    assert not state.has_pending_codes(NOW)
    await state.issue_code("dev1", "uk", NOW)
    assert state.has_pending_codes(NOW)
    assert not state.has_pending_codes(NOW + timedelta(minutes=11))


async def test_only_hash_of_code_is_stored(state, hass_storage):
    code = await state.issue_code("dev1", "uk", NOW)
    await _flush(None, state)
    assert code not in str(hass_storage)


async def test_set_bot_drops_chats(state):
    await state.claim_code(await state.issue_code("dev1", "uk", NOW), 42, None, NOW)
    await state.set_offset(7)
    await state.set_queue([{"x": 1}])
    await state.set_status("invalid_token")
    await state.issue_code("dev2", "uk", NOW)
    await state.set_bot("999999:ZZZdefGHIjklMNOpqrSTUvwxYZ0123456789", "other")
    assert state.chats() == []
    assert state.offset == 0 and state.queue() == [] and state.status is None
    assert not state.has_pending_codes(NOW)
    assert state.bot_username() == "other"


async def test_clear(state):
    await state.clear()
    assert state.token() is None and state.bot_username() is None


async def test_enable_unlink(state):
    await state.claim_code(await state.issue_code("dev1", "uk", NOW), 42, None, NOW)
    await state.set_enabled("dev1", False)
    assert not state.chat_for("dev1").enabled
    await state.unlink_chat_id(42)
    assert state.chat_for("dev1") is None
    await state.claim_code(await state.issue_code("dev1", "uk", NOW), 42, None, NOW)
    await state.unlink("dev1")
    assert state.chats() == []


async def test_token_never_in_repr(state):
    await state.claim_code(await state.issue_code("dev1", "uk", NOW), 42, None, NOW)
    assert TOKEN not in repr(state) and TOKEN not in str(state)
    assert TOKEN not in repr(state.chats()[0])


def test_parse_token_takes_token_out_of_botfather_message():
    msg = f"Done! Use this token to access the HTTP API:\n{TOKEN}\nKeep it secure."
    assert parse_token(msg) == TOKEN


@pytest.mark.parametrize("raw", ["", "hello", "123:short", "abc:" + "x" * 40])
def test_parse_token_rejects_garbage(raw):
    assert parse_token(raw) is None


async def test_state_survives_reload(hass, state):
    code = await state.issue_code("dev1", "uk", NOW)
    await state.claim_code(code, 42, "ivan", NOW)
    await state.issue_code("dev2", "en", NOW)
    await state.set_offset(5)
    await state.set_detector({"a": 1})
    await state.set_queue([{"text": "hi"}])
    await state.set_status("bot_in_use_elsewhere")
    await _flush(hass, state)

    other = TelegramState(hass)
    await other.load()
    assert other.token() == TOKEN and other.bot_username() == "my_bot"
    assert other.chat_for("dev1").chat_id == 42
    assert other.chat_for("dev1").username == "ivan"
    assert other.offset == 5
    assert other.detector() == {"a": 1}
    assert other.queue() == [{"text": "hi"}]
    assert other.status == "bot_in_use_elsewhere"
    assert other.has_pending_codes(NOW)
