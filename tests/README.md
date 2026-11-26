# Testing the SMS-XMPP Bridge

This directory contains the unit test suite for the SMS-XMPP bridge logic. The tests use `pytest`, `pytest-asyncio`, and `unittest.mock` to verify message parsing, authorization, webhook handling, and API interactions without requiring a live XMPP server or actual SMS credits.

## Test Coverage
- **UK Number Normalization & Parsing:** Verifies that UK mobile numbers (`07...`, `00447...`, `+447...`, spaced numbers) normalize to `+447...` across space, colon, newline, and pipe prefixes, while rejecting non-UK international prefixes and non-mobile numbers.
- **Confirmation & Sticky Sessions:** Tests two-step `y`/`n` confirmation prompts, 60-second confirmation expiry, 15-minute sticky reply timeouts, and multi-sender collision warnings.
- **Bill Protection:** Verifies GSM-7 vs. Unicode (UCS-2) segment/cost calculation, smart punctuation normalization, the 4-part message cap, and the 30 SMS/hour rate limit.
- **Authorization & Security:** Confirms unauthorized XMPP senders and presence subscriptions are rejected, OMEMO-encrypted messages are flagged, and webhook requests with invalid tokens return `403 Forbidden`.
- **Inbound Webhook & Outbound API:** Mocks AAISP webhook payloads and outbound HTTP POST requests, including `OK:` receipts and `ERR:` failure handling.


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
