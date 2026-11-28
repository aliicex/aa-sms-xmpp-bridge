import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import make_mocked_request

import src.bot as bot_module
from src.bot import (
    AALine,
    MAX_SMS_PER_HOUR,
    SMSBridgeBot,
    calculate_sms_parts,
    handle_inbound_sms,
    validate_config,
)


PERSONAL_LINE = AALine(
    alias="personal",
    number="+447700900001",
    username="+447700900001",
    password="test-pass-personal",
    originator="",
)

WORK_LINE = AALine(
    alias="work",
    number="+447700900002",
    username="+447700900001",
    password="test-pass-work",
    originator="+447700900002",
)


class MockXMPPMessage:
    """Mocks the slixmpp Message stanza object (supports dict-style access)."""

    def __init__(
        self,
        body: str,
        sender: str = "owner@test.im",
        msg_type: str = "chat",
        xml: list | None = None,
    ) -> None:
        self._data: dict[str, object] = {
            "body": body,
            "type": msg_type,
            "from": SimpleNamespace(bare=sender),
        }
        self.xml = xml or []
        self.reply_text: str | None = None
        self.sent: bool = False

    def __getitem__(self, key: str):
        return self._data[key]

    def reply(self, text: str) -> "MockXMPPMessage":
        self.reply_text = text
        return self

    def send(self) -> None:
        self.sent = True


@pytest.fixture(autouse=True)
def configure_test_env(monkeypatch, tmp_path):
    monkeypatch.setattr(bot_module, "TARGET_USER_JID", "owner@test.im")
    monkeypatch.setattr(bot_module, "WEBHOOK_TOKEN", "super-secret-token")
    monkeypatch.setattr(bot_module, "RATE_DB_PATH", str(tmp_path / "rate-limit.sqlite3"))
    monkeypatch.setattr(
        bot_module,
        "LINES_BY_ALIAS",
        {"personal": PERSONAL_LINE, "work": WORK_LINE},
    )
    monkeypatch.setattr(
        bot_module,
        "LINES_BY_NUMBER",
        {PERSONAL_LINE.number: PERSONAL_LINE, WORK_LINE.number: WORK_LINE},
    )
    monkeypatch.setattr(bot_module, "DEFAULT_LINE", PERSONAL_LINE)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_input,expected_line,expected_num,expected_text",
    [
        ("+447700900000 Hello from XMPP", PERSONAL_LINE, "+447700900000", "Hello from XMPP"),
        ("07700900000 Hello without plus", PERSONAL_LINE, "+447700900000", "Hello without plus"),
        ("07700 900 000: Spaced UK number", PERSONAL_LINE, "+447700900000", "Spaced UK number"),
        ("00447700900000 Double-zero UK", PERSONAL_LINE, "+447700900000", "Double-zero UK"),
        ("7700900000 Ten-digit UK", PERSONAL_LINE, "+447700900000", "Ten-digit UK"),
        ("07700900000\nNewline message", PERSONAL_LINE, "+447700900000", "Newline message"),
        ("+447700900000|Pipe fallback", PERSONAL_LINE, "+447700900000", "Pipe fallback"),
        ("work 07700900000 Hello from work", WORK_LINE, "+447700900000", "Hello from work"),
        ("work: +447700900000: Colon work format", WORK_LINE, "+447700900000", "Colon work format"),
    ],
)
async def test_uk_normalization_and_confirmation_flow(raw_input, expected_line, expected_num, expected_text):
    """UK numbers normalize to +447..., route via selected/default line, and dispatch on 'y'."""
    bot = SMSBridgeBot("bot@test.im", "password")
    bot.send_sms = AsyncMock()

    draft_msg = MockXMPPMessage(raw_input, sender="owner@test.im")
    bot.handle_message(draft_msg)
    await asyncio.sleep(0.02)

    bot.send_sms.assert_not_called()
    assert f"About to send message to {expected_num} via {expected_line.display_label}" in draft_msg.reply_text
    assert "1 part, ~3.6p" in draft_msg.reply_text
    assert expected_text in draft_msg.reply_text

    confirm_msg = MockXMPPMessage("y", sender="owner@test.im")
    bot.handle_message(confirm_msg)
    await asyncio.sleep(0.02)

    bot.send_sms.assert_called_once_with(
        expected_line,
        expected_num,
        expected_text,
        confirm_msg,
        reservation_id=1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "non_uk_input",
    [
        "+14155550199 Hello US number",
        "+33 6 12 34 56 78: Hello French number",
        "0014155550199 Double-zero US number",
        "work +14155550199 Hello US from work",
    ],
)
async def test_rejects_non_uk_international_numbers(non_uk_input):
    """Non-+44 international prefixes return 'Only UK numbers supported.'"""
    bot = SMSBridgeBot("bot@test.im", "password")
    bot.send_sms = AsyncMock()

    msg = MockXMPPMessage(non_uk_input, sender="owner@test.im")
    bot.handle_message(msg)
    await asyncio.sleep(0.02)

    bot.send_sms.assert_not_called()
    assert msg.reply_text == "Only UK numbers supported."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_uk_input",
    [
        "01234567890 Landline number",
        "077009000 Too short UK mobile",
        "07700900000123 Too long UK mobile",
    ],
)
async def test_rejects_invalid_uk_mobile_numbers(invalid_uk_input):
    """Malformed UK numbers or landlines return a UK mobile validation message."""
    bot = SMSBridgeBot("bot@test.im", "password")
    bot.send_sms = AsyncMock()

    msg = MockXMPPMessage(invalid_uk_input, sender="owner@test.im")
    bot.handle_message(msg)
    await asyncio.sleep(0.02)

    bot.send_sms.assert_not_called()
    assert "Invalid UK mobile number" in msg.reply_text


@pytest.mark.asyncio
async def test_line_alias_without_recipient_returns_usage_hint():
    """Typing '<alias> <message>' without a phone number prompts for the recipient."""
    bot = SMSBridgeBot("bot@test.im", "password")
    bot.send_sms = AsyncMock()

    msg = MockXMPPMessage("work Hello there", sender="owner@test.im")
    bot.handle_message(msg)
    await asyncio.sleep(0.02)

    bot.send_sms.assert_not_called()
    assert "Line 'work' specified without a recipient number" in msg.reply_text

@pytest.mark.asyncio
async def test_cancel_pending_sms_keeps_stickiness():
    """Replying 'n' cancels the staged SMS while keeping the active sticky session."""
    bot = SMSBridgeBot("bot@test.im", "password")
    bot.send_sms = AsyncMock()
    bot.record_inbound_session("07700900099", WORK_LINE)

    bot.handle_message(MockXMPPMessage("Typo message", sender="owner@test.im"))
    cancel_msg = MockXMPPMessage("n", sender="owner@test.im")
    bot.handle_message(cancel_msg)
    await asyncio.sleep(0.02)

    bot.send_sms.assert_not_called()
    assert cancel_msg.reply_text == "❌ Cancelled."
    assert bot.pending_sms is None
    assert ("work", "+447700900099") in bot.recent_sessions
    bot._clear_stickiness()


@pytest.mark.asyncio
async def test_confirmation_timeout_notifies_and_clears_stickiness(monkeypatch):
    """When confirmation times out, user is notified and stickiness is cleared."""
    monkeypatch.setattr(bot_module, "CONFIRM_TIMEOUT_SECONDS", 0.05)

    bot = SMSBridgeBot("bot@test.im", "password")
    bot.send_sms = AsyncMock()
    bot.send_message = MagicMock()
    bot.record_inbound_session("07700900099", WORK_LINE)

    bot.handle_message(MockXMPPMessage("On my way!", sender="owner@test.im"))
    await asyncio.sleep(0.1)

    bot.send_sms.assert_not_called()
    assert bot.pending_sms is None
    assert len(bot.recent_sessions) == 0

    bot.send_message.assert_called_once()
    sent_body = bot.send_message.call_args.kwargs["mbody"]
    assert "Confirmation timed out" in sent_body
    assert "via work" in sent_body
    assert "specify a recipient number" in sent_body


@pytest.mark.asyncio
async def test_sticky_timeout_notifies_and_clears_stickiness(monkeypatch):
    """When 15m stickiness expires, user is notified to specify a recipient next time."""
    monkeypatch.setattr(bot_module, "STICKY_TIMEOUT_SECONDS", 0.05)

    bot = SMSBridgeBot("bot@test.im", "password")
    bot.send_message = MagicMock()
    bot.record_inbound_session("07700900099", PERSONAL_LINE)

    await asyncio.sleep(0.1)

    assert len(bot.recent_sessions) == 0
    bot.send_message.assert_called_once()
    sent_body = bot.send_message.call_args.kwargs["mbody"]
    assert "expired" in sent_body
    assert "+447700900099 via personal" in sent_body
    assert "specify a recipient number" in sent_body


def test_sms_part_and_unicode_calculation():
    """GSM-7 allows 160 chars in 1 part; emojis trigger Unicode (70 UTF-16 units/part)."""
    assert calculate_sms_parts("a" * 160) == (1, False)
    assert calculate_sms_parts("a" * 161) == (2, False)
    assert calculate_sms_parts("Hello 😊") == (1, True)
    assert calculate_sms_parts("😊" * 35) == (1, True)
    assert calculate_sms_parts("😊" * 36) == (2, True)
    assert calculate_sms_parts("😊" * 71) == (3, True)


@pytest.mark.asyncio
async def test_smart_punctuation_normalized_and_unicode_warned():
    """Curly quotes normalize to GSM-7; emojis show a Unicode cost warning."""
    bot = SMSBridgeBot("bot@test.im", "password")
    bot.send_sms = AsyncMock()

    smart_msg = MockXMPPMessage("07700900000 It\u2019s great \u2014 really!", sender="owner@test.im")
    bot.handle_message(smart_msg)
    assert "1 part, ~3.6p):" in smart_msg.reply_text
    assert '"It\'s great - really!"' in smart_msg.reply_text

    emoji_msg = MockXMPPMessage("07700900000 Hey 🎉", sender="owner@test.im")
    bot.handle_message(emoji_msg)
    assert "Unicode/emoji detected" in emoji_msg.reply_text


@pytest.mark.asyncio
async def test_blocks_message_exceeding_max_parts():
    """Messages exceeding MAX_SMS_PARTS are blocked to prevent accidental high charges."""
    bot = SMSBridgeBot("bot@test.im", "password")
    bot.send_sms = AsyncMock()

    long_text = "a" * 700
    msg = MockXMPPMessage(f"07700900000 {long_text}", sender="owner@test.im")
    bot.handle_message(msg)
    await asyncio.sleep(0.02)

    bot.send_sms.assert_not_called()
    assert "Message too long" in msg.reply_text


@pytest.mark.asyncio
async def test_blocks_when_hourly_rate_limit_reached():
    """Outbound SMS is blocked once MAX_SMS_PER_HOUR slots are reserved in SQLite."""
    bot = SMSBridgeBot("bot@test.im", "password")
    bot.send_sms = AsyncMock()
    for _ in range(MAX_SMS_PER_HOUR):
        assert bot._reserve_hourly_slot() is not None
    assert bot._reserve_hourly_slot() is None

    msg = MockXMPPMessage("07700900000 Hello", sender="owner@test.im")
    bot.handle_message(msg)
    await asyncio.sleep(0.02)

    bot.send_sms.assert_not_called()
    assert msg.reply_text is not None
    assert "Hourly rate limit reached" in msg.reply_text


@pytest.mark.asyncio
async def test_detects_omemo_encrypted_message():
    """Encrypted XMPP stanzas prompt the user to disable OMEMO."""
    bot = SMSBridgeBot("bot@test.im", "password")
    bot.send_sms = AsyncMock()

    omemo_elem = SimpleNamespace(tag="{eu.siacs.conversations.axolotl}encrypted")
    msg = MockXMPPMessage("I sent you an OMEMO encrypted message", sender="owner@test.im", xml=[omemo_elem])
    bot.handle_message(msg)
    await asyncio.sleep(0.02)

    bot.send_sms.assert_not_called()
    assert "Encrypted XMPP message detected" in msg.reply_text


@pytest.mark.asyncio
async def test_status_lines_and_help_commands():
    """Built-in 'help', 'lines', and 'status' commands report usage, configured lines, and active session."""
    bot = SMSBridgeBot("bot@test.im", "password")

    help_msg = MockXMPPMessage("help", sender="owner@test.im")
    bot.handle_message(help_msg)
    assert "SMS Bridge Commands" in help_msg.reply_text

    lines_msg = MockXMPPMessage("lines", sender="owner@test.im")
    bot.handle_message(lines_msg)
    assert "personal: +447700900001 [DEFAULT]" in lines_msg.reply_text
    assert "work: +447700900002 (oa: +447700900002)" in lines_msg.reply_text

    status_empty = MockXMPPMessage("status", sender="owner@test.im")
    bot.handle_message(status_empty)
    assert "No active sticky reply session" in status_empty.reply_text

    bot.record_inbound_session("07700900099", WORK_LINE)
    status_active = MockXMPPMessage("status", sender="owner@test.im")
    bot.handle_message(status_active)
    assert "Active reply session: +447700900099 via work" in status_active.reply_text
    bot._clear_stickiness()

def test_presence_subscription_restricted_to_owner():
    """Only TARGET_USER_JID is approved for presence subscriptions."""
    bot = SMSBridgeBot("bot@test.im", "password")
    bot.send_presence = MagicMock()

    bot.handle_presence_subscribe({"from": SimpleNamespace(bare="owner@test.im")})
    bot.send_presence.assert_any_call(pto="owner@test.im", ptype="subscribed")

    bot.send_presence.reset_mock()
    bot.handle_presence_subscribe({"from": SimpleNamespace(bare="stranger@evil.im")})
    bot.send_presence.assert_called_once_with(pto="stranger@evil.im", ptype="unsubscribed")


@pytest.mark.asyncio
async def test_no_recipient_specified_without_active_session():
    """Message with no number and no active session instructs user to specify a UK number."""
    bot = SMSBridgeBot("bot@test.im", "password")
    bot.send_sms = AsyncMock()

    msg = MockXMPPMessage("Hello without a number", sender="owner@test.im")
    bot.handle_message(msg)
    await asyncio.sleep(0.02)

    bot.send_sms.assert_not_called()
    assert "No active conversation to reply to" in msg.reply_text
    assert "specify a UK recipient number" in msg.reply_text


@pytest.mark.asyncio
async def test_sticky_reply_uses_inbound_matched_line():
    """Plain text stages confirmation using the exact line that received the inbound SMS."""
    bot = SMSBridgeBot("bot@test.im", "password")
    bot.send_sms = AsyncMock()
    bot.record_inbound_session("07700900099", WORK_LINE)

    draft_msg = MockXMPPMessage("On my way!", sender="owner@test.im")
    bot.handle_message(draft_msg)
    assert "About to send message to +447700900099 via work (+447700900002)" in draft_msg.reply_text

    confirm_msg = MockXMPPMessage("y", sender="owner@test.im")
    bot.handle_message(confirm_msg)
    await asyncio.sleep(0.02)

    bot.send_sms.assert_called_once_with(
        WORK_LINE,
        "+447700900099",
        "On my way!",
        confirm_msg,
        reservation_id=1,
    )
    bot._clear_stickiness()


@pytest.mark.asyncio
async def test_sticky_reply_collision_guard_across_lines():
    """If two sessions are active within 15 minutes (even across different lines), sticky reply pauses."""
    bot = SMSBridgeBot("bot@test.im", "password")
    bot.send_sms = AsyncMock()
    bot.record_inbound_session("07700900011", PERSONAL_LINE)
    bot.record_inbound_session("07700900099", WORK_LINE)

    msg = MockXMPPMessage("Sounds good!", sender="owner@test.im")
    bot.handle_message(msg)
    await asyncio.sleep(0.02)

    bot.send_sms.assert_not_called()
    assert "Multiple recent sessions" in msg.reply_text
    assert "+447700900011 (via personal)" in msg.reply_text
    assert "+447700900099 (via work)" in msg.reply_text
    bot._clear_stickiness()


@pytest.mark.asyncio
async def test_handle_message_unauthorized_sender():
    """Messages from anyone other than TARGET_USER_JID must be ignored."""
    bot = SMSBridgeBot("bot@test.im", "password")
    bot.send_sms = AsyncMock()

    msg = MockXMPPMessage("07700900000 Free SMS!", sender="stranger@evil.im")
    bot.handle_message(msg)
    await asyncio.sleep(0.02)

    bot.send_sms.assert_not_called()
    assert msg.reply_text is None


@pytest.mark.asyncio
async def test_send_sms_api_success_and_originator_payload():
    """HTTP 200 without ERR prefix dispatches SMS with line credentials and optional originator."""
    bot = SMSBridgeBot("bot@test.im", "password")
    orig_msg = MockXMPPMessage("y")

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.text = AsyncMock(return_value="OK: Message queued")

    mock_post_ctx = AsyncMock()
    mock_post_ctx.__aenter__.return_value = mock_response

    bot.http_session = MagicMock()
    bot.http_session.post.return_value = mock_post_ctx

    # 1. Line without originator omits 'oa'
    await bot.send_sms(PERSONAL_LINE, "+447700900000", "Personal Msg", orig_msg)
    args, kwargs = bot.http_session.post.call_args
    assert "sms.aa.net.uk/sms.cgi" in args
    assert kwargs["data"]["username"] == PERSONAL_LINE.username
    assert kwargs["data"]["password"] == PERSONAL_LINE.password
    assert kwargs["data"]["da"] == "+447700900000"
    assert kwargs["data"]["ud"] == "Personal Msg"
    assert "oa" not in kwargs["data"]
    assert orig_msg.reply_text == "✓ Sent to +447700900000 via personal"

    # 2. Line with originator includes 'oa'
    await bot.send_sms(WORK_LINE, "+447700900000", "Work Msg", orig_msg)
    _, kwargs_work = bot.http_session.post.call_args
    assert kwargs_work["data"]["password"] == WORK_LINE.password
    assert kwargs_work["data"]["oa"] == "+447700900002"
    assert orig_msg.reply_text == "✓ Sent to +447700900000 via work"

@pytest.mark.asyncio
async def test_webhook_inbound_matches_line_and_sets_sticky_sender():
    """Valid webhook POST matches 'da' to configured line, forwards to XMPP, and sets sticky line."""
    bot = SMSBridgeBot("bot@test.im", "password")
    bot.send_message = MagicMock()

    request = make_mocked_request(
        "POST",
        "/webhook?token=super-secret-token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        app={"bot": bot},
    )

    request.post = AsyncMock(
        return_value={
            "oa": "07700900099",
            "da": "+447700900002",
            "ud": "Hello work line!",
        }
    )

    response = await handle_inbound_sms(request)

    assert response.status == 200
    bot.send_message.assert_called_once()
    call_kwargs = bot.send_message.call_args.kwargs
    assert call_kwargs["mto"] == "owner@test.im"
    assert "[work]" in call_kwargs["mbody"]
    assert "+447700900099" in call_kwargs["mbody"]
    assert "Reply directly to send to +447700900099 via work" in call_kwargs["mbody"]
    assert ("work", "+447700900099") in bot.recent_sessions
    bot._clear_stickiness()


@pytest.mark.asyncio
async def test_webhook_inbound_unconfigured_recipient_does_not_set_stickiness():
    """Inbound SMS to a 'da' not in AA_LINE_* warns the user and avoids setting sticky state."""
    bot = SMSBridgeBot("bot@test.im", "password")
    bot.send_message = MagicMock()

    request = make_mocked_request(
        "POST",
        "/webhook?token=super-secret-token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        app={"bot": bot},
    )

    request.post = AsyncMock(
        return_value={
            "oa": "07700900099",
            "da": "+447700999999",
            "ud": "Who is this?",
        }
    )

    response = await handle_inbound_sms(request)

    assert response.status == 200
    assert len(bot.recent_sessions) == 0
    sent_body = bot.send_message.call_args.kwargs["mbody"]
    assert "[unmatched line]" in sent_body
    assert "not configured in secrets.env" in sent_body


@pytest.mark.asyncio
async def test_webhook_inbound_invalid_token():
    """Invalid webhook token returns 403 Forbidden without forwarding."""
    bot = SMSBridgeBot("bot@test.im", "password")
    bot.send_message = MagicMock()

    request = make_mocked_request(
        "POST",
        "/webhook?token=wrong-token",
        app={"bot": bot},
    )

    response = await handle_inbound_sms(request)

    assert response.status == 403
    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_send_sms_retains_slot_on_success_and_releases_on_api_error():
    """Successful sends keep the hourly SQLite slot; AAISP ERR responses release it."""
    bot = SMSBridgeBot("bot@test.im", "password")
    orig_msg = MockXMPPMessage("y")

    # 1. API error releases slot
    err_resp = AsyncMock()
    err_resp.status = 200
    err_resp.text = AsyncMock(return_value="ERR: Invalid password")
    err_ctx = AsyncMock()
    err_ctx.__aenter__.return_value = err_resp

    bot.http_session = MagicMock()
    bot.http_session.post.return_value = err_ctx

    res_id_1 = bot._reserve_hourly_slot()
    assert res_id_1 is not None
    await bot.send_sms(PERSONAL_LINE, "+447700900000", "Fail test", orig_msg, reservation_id=res_id_1)
    assert bot._has_available_hourly_slot()

    # 2. Successful send retains slot until hourly limit is reached
    ok_resp = AsyncMock()
    ok_resp.status = 200
    ok_resp.text = AsyncMock(return_value="OK: Sent")
    ok_ctx = AsyncMock()
    ok_ctx.__aenter__.return_value = ok_resp
    bot.http_session.post.return_value = ok_ctx

    for _ in range(MAX_SMS_PER_HOUR):
        rid = bot._reserve_hourly_slot()
        assert rid is not None
        await bot.send_sms(PERSONAL_LINE, "+447700900000", "Success test", orig_msg, reservation_id=rid)

    assert not bot._has_available_hourly_slot()


@pytest.mark.asyncio
async def test_send_sms_retains_slot_on_network_error():
    """Network exceptions leave outcome unknown and keep the slot to protect balance."""
    bot = SMSBridgeBot("bot@test.im", "password")
    orig_msg = MockXMPPMessage("y")

    bot.http_session = MagicMock()
    bot.http_session.post.side_effect = RuntimeError("Connection reset")

    for _ in range(MAX_SMS_PER_HOUR):
        rid = bot._reserve_hourly_slot()
        assert rid is not None
        await bot.send_sms(PERSONAL_LINE, "+447700900000", "Timeout test", orig_msg, reservation_id=rid)

    assert not bot._has_available_hourly_slot()
    assert orig_msg.reply_text is not None
    assert "Error sending SMS" in orig_msg.reply_text


@pytest.mark.asyncio
async def test_webhook_rejects_unsupported_media_type():
    """Webhook rejects non-form Content-Type headers with HTTP 415."""
    bot = SMSBridgeBot("bot@test.im", "password")
    request = make_mocked_request(
        "POST",
        "/webhook?token=super-secret-token",
        headers={"Content-Type": "application/json"},
        app={"bot": bot},
    )

    response = await handle_inbound_sms(request)
    assert response.status == 415


def test_validate_config_parses_flat_aa_line_env_vars(monkeypatch):
    """validate_config discovers AA_LINE_<N>_* variables and sets DEFAULT_LINE."""
    monkeypatch.setattr(bot_module, "XMPP_JID", "bot@test.im")
    monkeypatch.setattr(bot_module, "XMPP_PASSWORD", "bot-pass")
    monkeypatch.setattr(bot_module, "WEBHOOK_TOKEN", "secret-token")
    monkeypatch.setattr(bot_module, "TARGET_USER_JID", "owner@test.im")
    monkeypatch.setattr(bot_module, "DEFAULT_LINE_ALIAS", "work")
    monkeypatch.setattr(bot_module, "LINES_BY_ALIAS", {})
    monkeypatch.setattr(bot_module, "LINES_BY_NUMBER", {})
    monkeypatch.setattr(bot_module, "DEFAULT_LINE", None)

    monkeypatch.setenv("AA_LINE_1_ALIAS", "personal")
    monkeypatch.setenv("AA_LINE_1_NUMBER", "07700900001")
    monkeypatch.setenv("AA_LINE_1_USERNAME", "+447700900001")
    monkeypatch.setenv("AA_LINE_1_PASSWORD", "pass1")

    monkeypatch.setenv("AA_LINE_2_ALIAS", "work")
    monkeypatch.setenv("AA_LINE_2_NUMBER", "+447700900002")
    monkeypatch.setenv("AA_LINE_2_USERNAME", "+447700900001")
    monkeypatch.setenv("AA_LINE_2_PASSWORD", "pass2")
    monkeypatch.setenv("AA_LINE_2_ORIGINATOR", "+447700900002")

    validate_config()

    assert set(bot_module.LINES_BY_ALIAS.keys()) == {"personal", "work"}
    assert bot_module.DEFAULT_LINE == bot_module.LINES_BY_ALIAS["work"]
    assert bot_module.LINES_BY_NUMBER["+447700900002"].originator == "+447700900002"
