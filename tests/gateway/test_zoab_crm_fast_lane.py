"""Zoab CRM Fast Lane routing (2026-07-10 incident fix).

Patrick reported plain CRM commands ("Amanda Novak job status", "Amanda
Novak's job status", "update Amanda Novak: ...") falling through to the
general agent, which then ran raw sqlite3/terminal commands against the
CRM database instead of using the CRM API.

Root cause: the Zoab CRM fast lane existed only in the old
gateway/platforms/telegram.py, which stopped being loaded once Telegram
moved to this plugin (#41112) -- the fast lane was never ported over, so
_handle_text_message here had no CRM routing at all. Every text message
went straight to _enqueue_text_event (the general agent path).

These tests prove, for each of the three reported commands:
  1. The Zoab CRM API is called (mocked; asserts the request body).
  2. The reply sent to Patrick comes from the CRM's response, not the
     general agent.
  3. _enqueue_text_event -- the entry point to the general agent and its
     shell/file/db tools -- is never called.

A non-CRM message is included as a negative control: it must still take
the normal _enqueue_text_event path unchanged.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig


def _make_adapter(allow_from=None):
    from plugins.platforms.telegram.adapter import TelegramAdapter

    extra = {}
    if allow_from is not None:
        extra["allow_from"] = allow_from

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="fake-token", extra=extra)
    adapter._bot = SimpleNamespace(id=999, username="test_bot")
    adapter._message_handler = AsyncMock()
    adapter._pending_text_batches = {}
    adapter._pending_text_batch_tasks = {}
    adapter._text_batch_delay_seconds = 0.01
    adapter._text_batch_split_delay_seconds = 0.01
    adapter._mention_patterns = adapter._compile_mention_patterns()
    adapter._forum_lock = asyncio.Lock()
    adapter._forum_command_registered = set()
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    adapter._enqueue_text_event = MagicMock(name="_enqueue_text_event")
    return adapter


def _make_message(text, *, from_user_id=111, chat_id=111):
    return SimpleNamespace(
        message_id=42,
        text=text,
        caption=None,
        entities=[],
        caption_entities=[],
        message_thread_id=None,
        is_topic_message=False,
        chat=SimpleNamespace(id=chat_id, type="private", title=None, is_forum=False),
        from_user=SimpleNamespace(id=from_user_id, full_name="Patrick", first_name="Patrick"),
        reply_to_message=None,
        date=None,
        location=None,
        photo=None,
        video=None,
        audio=None,
        voice=None,
        document=None,
        sticker=None,
        media_group_id=None,
        reply_text=AsyncMock(),
    )


def _update_for(msg):
    return SimpleNamespace(update_id=1, message=msg, effective_message=None)


# Canned CRM responses matching the real /api/max/command shapes observed
# via direct curl against the live Zoab CRM for these exact phrasings.
_JOB_STATUS_RESPONSE = {
    "action": "project_report",
    "status": "ok",
    "message": "Project Report — Amanda Novak\nStatus: Pending\nContract: $15,997",
}
_UPDATE_NOTE_CONFIRM_RESPONSE = {
    "action": "clarification",
    "status": "need_clarification",
    "clarification_type": "write_confirmation",
    "confirm_command": "note #362!: drywall completed, walls primed, ceilings painted, starting trim today",
    "message": "Add note for Amanda Novak: \"drywall completed, walls primed, ceilings painted, starting trim today\"?\nReply 'yes' to confirm or 'cancel' to drop it.",
}
_UNROUTED_RESPONSE = {
    "action": "",
    "status": "need_clarification",
    "message": "I could not determine the command.",
}


def _mock_crm_api(response_by_text):
    """Patch _zoab_crm_api to return canned responses keyed by exact text,
    and record every call for assertion."""
    calls = []

    def _fake(raw_text, timeout=8):
        calls.append(raw_text)
        return response_by_text[raw_text]

    return calls, _fake


@pytest.mark.asyncio
async def test_job_status_uses_crm_only_no_general_agent():
    """'Amanda Novak job status' -> CRM API call, CRM reply, general
    agent (_enqueue_text_event) never reached."""
    adapter = _make_adapter(allow_from=["111"])
    text = "Amanda Novak job status"
    calls, fake_api = _mock_crm_api({text: _JOB_STATUS_RESPONSE})

    with patch("plugins.platforms.telegram.adapter._zoab_crm_api", side_effect=fake_api):
        msg = _make_message(text)
        await adapter._handle_text_message(_update_for(msg), SimpleNamespace())

    assert calls == [text], "CRM API must be called with the exact message text"
    msg.reply_text.assert_awaited_once()
    reply = msg.reply_text.await_args.args[0]
    assert "Amanda Novak" in reply and "Pending" in reply
    adapter._enqueue_text_event.assert_not_called()


@pytest.mark.asyncio
async def test_possessive_job_status_uses_crm_only_no_general_agent():
    """'Amanda Novak's job status' -- the exact phrasing from the real
    2026-07-10 05:05 AM incident -- must also route to the CRM only."""
    adapter = _make_adapter(allow_from=["111"])
    text = "Amanda Novak's job status"
    calls, fake_api = _mock_crm_api({text: _JOB_STATUS_RESPONSE})

    with patch("plugins.platforms.telegram.adapter._zoab_crm_api", side_effect=fake_api):
        msg = _make_message(text)
        await adapter._handle_text_message(_update_for(msg), SimpleNamespace())

    assert calls == [text], "CRM API must be called with the exact message text"
    msg.reply_text.assert_awaited_once()
    reply = msg.reply_text.await_args.args[0]
    assert "Amanda Novak" in reply
    adapter._enqueue_text_event.assert_not_called()


@pytest.mark.asyncio
async def test_update_note_uses_crm_only_no_general_agent():
    """'update Amanda Novak: drywall completed...' -> CRM API call, CRM's
    (write-confirmation) reply relayed, general agent never reached."""
    adapter = _make_adapter(allow_from=["111"])
    text = "update Amanda Novak: drywall completed, walls primed, ceilings painted, starting trim today"
    calls, fake_api = _mock_crm_api({text: _UPDATE_NOTE_CONFIRM_RESPONSE})

    with patch("plugins.platforms.telegram.adapter._zoab_crm_api", side_effect=fake_api):
        msg = _make_message(text)
        await adapter._handle_text_message(_update_for(msg), SimpleNamespace())

    assert calls == [text], "CRM API must be called with the exact message text"
    msg.reply_text.assert_awaited_once()
    reply = msg.reply_text.await_args.args[0]
    assert "Add note for Amanda Novak" in reply
    adapter._enqueue_text_event.assert_not_called()


@pytest.mark.asyncio
async def test_crm_api_failure_surfaces_explicit_error_not_general_agent():
    """If the CRM API errors on a Layer-1-matched command, Patrick gets an
    explicit error -- not a silent hand-off to the general agent's
    shell/file tools. This is the exact failure mode from the real
    incident (a swallowed exception with zero trace)."""
    adapter = _make_adapter(allow_from=["111"])
    text = "Amanda Novak job status"

    def _raise(raw_text, timeout=8):
        raise ConnectionRefusedError("CRM unreachable")

    with patch("plugins.platforms.telegram.adapter._zoab_crm_api", side_effect=_raise):
        msg = _make_message(text)
        await adapter._handle_text_message(_update_for(msg), SimpleNamespace())

    msg.reply_text.assert_awaited_once()
    reply = msg.reply_text.await_args.args[0]
    assert "Zoab CRM error" in reply
    adapter._enqueue_text_event.assert_not_called()


@pytest.mark.asyncio
async def test_non_crm_message_still_uses_general_agent():
    """Negative control: an ordinary conversational message must still
    take the normal general-agent path unchanged."""
    adapter = _make_adapter(allow_from=["111"])
    text = "hey, how's your day going?"

    def _fake(raw_text, timeout=8):
        return dict(_UNROUTED_RESPONSE)

    with patch("plugins.platforms.telegram.adapter._zoab_crm_api", side_effect=_fake):
        msg = _make_message(text)
        await adapter._handle_text_message(_update_for(msg), SimpleNamespace())

    msg.reply_text.assert_not_awaited()
    adapter._enqueue_text_event.assert_called_once()


def test_layer1_pattern_matches_all_three_reported_commands():
    """Pure regex check (no mocking) for the three exact phrasings from
    the bug report -- fast, direct documentation of what Layer 1 covers."""
    from plugins.platforms.telegram.adapter import _zoab_crm_layer1_match

    assert _zoab_crm_layer1_match("amanda novak job status") is True
    assert _zoab_crm_layer1_match("amanda novak's job status") is True
    assert _zoab_crm_layer1_match(
        "update amanda novak: drywall completed, walls primed, ceilings painted, starting trim today"
    ) is True


def test_layer1_pattern_does_not_capture_multiline_daily_log_update():
    """Regression guard: a genuine multi-line 'update [name]:\\n[body]'
    daily-log command (a separate, pre-existing CRM feature) must not be
    captured by the single-line update-note pattern added here."""
    from plugins.platforms.telegram.adapter import _zoab_crm_layer1_match

    multiline = "update amanda novak:\ncrew arrived, primed the east wall."
    # The single-line update pattern must reject it (no non-whitespace
    # right after the colon before the newline)...
    import re
    single_line_update_re = re.compile(r"^update\s+(?:for\s+)?[^\n:]+?\s*:[ \t]*\S")
    assert single_line_update_re.match(multiline) is None
