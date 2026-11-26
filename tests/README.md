# Testing the SMS-XMPP Bridge

This directory contains the test suite for the XMPP-SMS bridge logic. The tests use `pytest` and `unittest.mock` to verify message parsing and API interaction without requiring a live XMPP server or actual SMS credits.

## Test Coverage
- **Message Parsing:** Ensures the bot correctly splits `number|message` and ignores invalid formats.
- **XMPP Interaction:** Verifies that the bot sends the correct help responses to users.
- **API Integration:** Mocks the Andrews & Arnold HTTP POST requests to ensure payloads are formatted correctly.

## Setup

### 1. Create a Test Environment
It is recommended to run tests in a virtual environment to avoid dependency conflicts:

`python3 -m venv venv`
`source venv/bin/activate`

### 2. Install Test Dependencies
Install the application dependencies along with the testing framework:

`pip install pytest pytest-asyncio aiohttp slixmpp`

## Running the Tests

### Run all tests
Execute the following command from the root of the repository:

`pytest tests/test_bot.py`

### Run with verbose output
To see exactly which tests are passing and their detailed logs:

`pytest -v tests/test_bot.py`

## Adding New Tests
When adding new features to `bot.py`, please add corresponding test cases in `test_bot.py`. 

Since the bot is asynchronous, ensure that:
1. Your test functions are decorated with `@pytest.mark.asyncio`.
2. Network calls are mocked using `unittest.mock.patch` or `AsyncMock` to prevent real API calls.
