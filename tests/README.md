# Testing the SMS-XMPP Bridge

This directory contains the unit test suite for the SMS-XMPP bridge logic. The tests use `pytest`, `pytest-asyncio`, and `unittest.mock` to verify message parsing, authorization, webhook handling, and API interactions without requiring a live XMPP server or actual SMS credits.

## Test Coverage
- **Message Parsing & Validation:** Ensures the bot strips spaces from numbers, splits `number|message`, and rejects malformed inputs.
- **Authorization & Security:** Verifies that unauthorized XMPP senders are ignored and webhook requests with invalid or missing tokens return `403 Forbidden`.
- **Inbound Webhook:** Confirms valid POST payloads from Andrews & Arnold are formatted and forwarded to `TARGET_USER_JID`.
- **Outbound API Integration:** Mocks Andrews & Arnold HTTP POST requests to verify payload formatting and error replies when AAISP returns `ERR:`.

## Setup

### 1. Create a Test Environment
Create and activate a virtual environment from the root of the repository:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install Dependencies
Install the application and testing dependencies:

```bash
pip install pytest pytest-asyncio aiohttp slixmpp python-dotenv
```

## Running the Tests

### Run all tests
Execute from the root of the repository:

```bash
python -m pytest
```

### Run with verbose output
To see individual test names and detailed output:

```bash
python -m pytest -v
```

## Adding New Tests
When adding new features to `src/bot.py`, add corresponding test cases in `tests/test_bot.py`:
1. Decorate async test functions with `@pytest.mark.asyncio`.
2. Mock network and XMPP calls using `AsyncMock` or `MagicMock` so tests run offline.
