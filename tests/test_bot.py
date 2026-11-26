import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from src.bot import SMSBridgeBot

# --- Mocks for XMPP Message Object ---
class MockXMPPMessage:
    """Mocks the slixmpp Message object"""
    def __init__(self, body, msg_type='chat'):
        self.body = body
        self.type = msg_type

    def reply(self, text):
        # Return self to allow chaining .reply().send()
        self.reply_text = text
        return self

    def send(self):
        # Mock the sending action
        pass

# --- Tests ---

@pytest.mark.asyncio
async def test_handle_message_valid_sms():
    """Test that a message in 'number|text' format triggers send_sms"""
    # Initialize bot without actually connecting to a server
    bot = SMSBridgeBot("bot@test.im", "password")

    # Mock the send_sms method to avoid actual API calls
    bot.send_sms = AsyncMock()

    # Simulate an XMPP message: "+44123456789|Hello from XMPP"
    msg = MockXMPPMessage("+44123456789|Hello from XMPP")

    # We call the handler. Note: bot.handle_message is a sync function
    # that creates an async task.
    bot.handle_message(msg)

    # Give the event loop a moment to process the created task
    await asyncio.sleep(0.1)

    # Verify the bot attempted to send the SMS with correct arguments
    bot.send_sms.assert_called_once_with("+44123456789", "Hello from XMPP")

@pytest.mark.asyncio
async def test_handle_message_invalid_format():
    """Test that a message without the '|' separator sends a help reply"""
    bot = SMSBridgeBot("bot@test.im", "password")
    bot.send_sms = AsyncMock()

    # Simulate a message that is just text
    msg = MockXMPPMessage("Hello, are you there?")

    bot.handle_message(msg)
    await asyncio.sleep(0.1)

    # Verify send_sms was NOT called
    bot.send_sms.assert_not_called()
    # Verify the bot replied with instructions
    assert "use format: +44" in msg.reply_text

@pytest.mark.asyncio
async def test_send_sms_api_success():
    """Test the HTTP POST request to AAISP API on success"""
    bot = SMSBridgeBot("bot@test.im", "password")

    # Mock aiohttp ClientSession.post to return a 200 OK
    with patch('aiohttp.ClientSession.post') as mock_post:
        mock_response = AsyncMock()
        mock_response.status = 200
        # Mock the context manager (async with session.post(...) as resp)
        mock_post.return_value.__aenter__.return_value = mock_response

        await bot.send_sms("+44123456789", "Test Message")

        # Ensure the API was called with the correct URL
        args, kwargs = mock_post.call_args
        assert "sms.aa.net.uk/sms.cgi" in args[0]
        assert kwargs['data']['da'] == "+44123456789"
        assert kwargs['data']['ud'] == "Test Message"

@pytest.mark.asyncio
async def test_send_sms_api_failure():
    """Test the bot's behavior when the AAISP API returns an error"""
    bot = SMSBridgeBot("bot@test.im", "password")

    with patch('aiohttp.ClientSession.post') as mock_post:
        mock_response = AsyncMock()
        mock_response.status = 500 # Internal Server Error
        mock_post.return_value.__aenter__.return_value = mock_response

        # This should not crash the bot, just log an error
        await bot.send_sms("+44123456789", "Test Message")

        mock_post.assert_called_once()
