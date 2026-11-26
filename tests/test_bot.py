import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import make_mocked_request

import src.bot as bot_module
from src.bot import SMSBridgeBot, handle_inbound_sms




class MockXMPPMessage:
    """Mocks the slixmpp Message stanza object (supports dict-style access)."""

    def __init__(self, body, sender="owner@test.im", msg_type="chat"):
        self._data = {
            "body": body,
            "type": msg_type,
            "from": SimpleNamespace(bare=sender),
        }
        self.reply_text = None
        self.sent = False

    def __getitem__(self, key):
        return self._data[key]

    def reply(self, text):
        self.reply_text = text
        return self

    def send(self):
        self.sent = True


@pytest.fixture(autouse=True)
def configure_test_env(monkeypatch):
    monkeypatch.setattr(bot_module, "TARGET_USER_JID", "owner@test.im")
    monkeypatch.setattr(bot_module, "WEBHOOK_TOKEN", "super-secret-token")
    monkeypatch.setattr(bot_module, "AA_USERNAME", "+447700900001")
    monkeypatch.setattr(bot_module, "AA_PASSWORD", "test-pass")


@pytest.mark.asyncio
async def test_handle_message_valid_sms():
    """Authorized sender using 'number|text' triggers send_sms with stripped spaces."""
    bot = SMSBridgeBot("bot@test.im", "password")
    bot.send_sms = AsyncMock()

    msg = MockXMPPMessage("+44 7700 900000 | Hello from XMPP", sender="owner@test.im")
    bot.handle_message(msg)
    await asyncio.sleep(0.05)

    bot.send_sms.assert_called_once_with("+447700900000", "Hello from XMPP", msg)


@pytest.mark.asyncio
async def test_handle_message_unauthorized_sender():
    """Messages from anyone other than TARGET_USER_JID must be ignored."""
    bot = SMSBridgeBot("bot@test.im", "password")
    bot.send_sms = AsyncMock()

    msg = MockXMPPMessage("+447700900000|Free SMS!", sender="stranger@evil.im")
    bot.handle_message(msg)
    await asyncio.sleep(0.05)

    bot.send_sms.assert_not_called()
    assert msg.reply_text is None


@pytest.mark.asyncio
async def test_handle_message_invalid_format():
    """Messages missing the '|' separator send a usage hint."""
    bot = SMSBridgeBot("bot@test.im", "password")
    bot.send_sms = AsyncMock()

    msg = MockXMPPMessage("Hello, are you there?", sender="owner@test.im")
    bot.handle_message(msg)
    await asyncio.sleep(0.05)

    bot.send_sms.assert_not_called()
    assert "use format: +44" in msg.reply_text
    assert msg.sent is True


@pytest.mark.asyncio
async def test_send_sms_api_success():
    """HTTP 200 without ERR prefix dispatches SMS without sending an error reply."""
    bot = SMSBridgeBot("bot@test.im", "password")
    orig_msg = MockXMPPMessage("+447700900000|Test Message")

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.text = AsyncMock(return_value="OK: Message queued")

    mock_post_ctx = AsyncMock()
    mock_post_ctx.__aenter__.return_value = mock_response

    bot.http_session = MagicMock()
    bot.http_session.post.return_value = mock_post_ctx

    await bot.send_sms("+447700900000", "Test Message", orig_msg)

    args, kwargs = bot.http_session.post.call_args
    assert "sms.aa.net.uk/sms.cgi" in args[0]
    assert kwargs["data"]["da"] == "+447700900000"
    assert kwargs["data"]["ud"] == "Test Message"
    assert orig_msg.reply_text is None


@pytest.mark.asyncio
async def test_send_sms_api_aaisp_err_response():
    """AAISP returns HTTP 200 with 'ERR:' on auth/number failure; bot must notify user."""
    bot = SMSBridgeBot("bot@test.im", "password")
    orig_msg = MockXMPPMessage("+447700900000|Test Message")

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.text = AsyncMock(return_value="ERR: Invalid destination")

    mock_post_ctx = AsyncMock()
    mock_post_ctx.__aenter__.return_value = mock_response

    bot.http_session = MagicMock()
    bot.http_session.post.return_value = mock_post_ctx

    await bot.send_sms("+447700900000", "Test Message", orig_msg)

    assert "Failed to send SMS: ERR: Invalid destination" in orig_msg.reply_text
    assert orig_msg.sent is True


@pytest.mark.asyncio
async def test_webhook_inbound_valid_token():
    """Valid webhook POST forwards SMS to TARGET_USER_JID and returns 200 OK."""
    bot = SMSBridgeBot("bot@test.im", "password")
    bot.send_message = MagicMock()

    request = make_mocked_request(
        "POST",
        "/webhook?token=super-secret-token",
        app={"bot": bot},
    )
    request.post = AsyncMock(
        return_value={
            "oa": "+447700900099",
            "da": "+447700900001",
            "ud": "Hello from mobile!",
        }
    )

    response = await handle_inbound_sms(request)

    assert response.status == 200
    bot.send_message.assert_called_once()
    call_kwargs = bot.send_message.call_args.kwargs
    assert call_kwargs["m_to"] == "owner@test.im"
    assert "+447700900099" in call_kwargs["m_body"]
    assert "Hello from mobile!" in call_kwargs["m_body"]


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
