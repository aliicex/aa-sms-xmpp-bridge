import asyncio
import hmac
import logging
import math
import os
import re
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from slixmpp import ClientXMPP

# Load secrets if running manually outside systemd (systemd loads EnvironmentFile automatically)
load_dotenv('/etc/sms-xmpp-bridge/secrets.env')

XMPP_JID = os.getenv('XMPP_JID', '').strip()
XMPP_PASSWORD = os.getenv('XMPP_PASSWORD', '').strip()
AA_USERNAME = os.getenv('AA_USERNAME', '').strip()
AA_PASSWORD = os.getenv('AA_PASSWORD', '').strip()
WEBHOOK_TOKEN = os.getenv('WEBHOOK_TOKEN', '').strip()
TARGET_USER_JID = os.getenv('TARGET_USER_JID', '').strip()
AA_API_URL = "https://sms.aa.net.uk/sms.cgi"

STICKY_TIMEOUT_SECONDS = 15 * 60  # 15 minutes
CONFIRM_TIMEOUT_SECONDS = 60      # 60 seconds to confirm outbound SMS
MAX_SMS_PARTS = 4                 # Hard cap per outbound SMS
MAX_SMS_PER_HOUR = 30             # Hourly outbound rate limit
COST_PER_PART_PENCE = 3.6         # AAISP standard UK outbound SMS tariff

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("SMSBridge")

# UK mobile numbers in E.164 format (+447 followed by 9 digits)
UK_E164_MOBILE_RE = re.compile(r"^\+447\d{9}$")

# Matches explicit delimiter formats: "<number>|<msg>", "<number>:<msg>", or "<number>\n<msg>"
EXPLICIT_DELIM_RE = re.compile(r"^\s*([+\d][\d\s\-]{6,22})\s*[:|\n]\s*(.+)$", re.DOTALL)
# Matches space-separated compact numbers: "07700900000 <msg>" or "+447700900000 <msg>"
SPACE_DELIM_RE = re.compile(r"^\s*([+\d]\d{6,15})\s+(.+)$", re.DOTALL)

# Standard GSM-7 basic + extended character sets (avoids costly UCS-2 Unicode encoding)
GSM7_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_EXTENDED = set("^{}\\[~]|€")

SMART_PUNCTUATION_MAP = str.maketrans({
    "\u2018": "'",    # ‘
    "\u2019": "'",    # ’
    "\u201c": '"',    # “
    "\u201d": '"',    # ”
    "\u2013": "-",    # – (en dash)
    "\u2014": "-",    # — (em dash)
    "\u2026": "...",  # … (ellipsis)
    "\u00a0": " ",    # non-breaking space
})


@dataclass
class PendingSMS:
    number: str
    text: str
    parts: int
    is_unicode: bool
    created_at: float
    timer_task: asyncio.Task | None = None


def normalize_smart_punctuation(text: str) -> str:
    """Replaces mobile-keyboard curly quotes/dashes with GSM-7 equivalents."""
    return text.translate(SMART_PUNCTUATION_MAP)


def calculate_sms_parts(text: str) -> tuple[int, bool]:
    """
    Calculates SMS segment count and whether Unicode (UCS-2) encoding is triggered.
    GSM-7: 160 chars for 1 part, 153 chars/part for multipart (extended chars count as 2).
    UCS-2: 70 chars for 1 part, 67 chars/part for multipart.
    """
    gsm_units = 0
    is_unicode = False

    for ch in text:
        if ch in GSM7_BASIC:
            gsm_units += 1
        elif ch in GSM7_EXTENDED:
            gsm_units += 2
        else:
            is_unicode = True
            break

    if is_unicode:
        length = len(text)
        if length <= 70:
            return 1, True
        return math.ceil(length / 67), True

    if gsm_units <= 160:
        return 1, False
    return math.ceil(gsm_units / 153), False


def strip_number_chars(raw_number: str) -> str:
    return re.sub(r"[\s\-]", "", raw_number).strip()


def normalize_uk_number(raw_number: str) -> tuple[str | None, str | None]:
    """
    Normalizes UK mobile numbers to +447XXXXXXXXX.
    Rejects non-UK international numbers with 'Only UK numbers supported.'
    Returns (normalized_e164_number, error_message).
    """
    cleaned = strip_number_chars(raw_number)

    if cleaned.startswith("+") and not cleaned.startswith("+44"):
        return None, "Only UK numbers supported."
    if cleaned.startswith("00") and not cleaned.startswith("0044"):
        return None, "Only UK numbers supported."

    if cleaned.startswith("0044"):
        normalized = "+44" + cleaned[4:]
    elif cleaned.startswith("+44"):
        normalized = cleaned
    elif cleaned.startswith("0"):
        normalized = "+44" + cleaned[1:]
    elif cleaned.startswith("7") and len(cleaned) == 10:
        normalized = "+44" + cleaned
    else:
        return None, (
            f"Invalid UK mobile number '{cleaned}'. "
            f"Use a UK mobile format (e.g., 07700900000 or +447700900000)."
        )

    if not UK_E164_MOBILE_RE.match(normalized):
        return None, (
            f"Invalid UK mobile number '{cleaned}'. "
            f"Expected an 11-digit UK mobile starting with 07 or +447."
        )

    return normalized, None


def parse_outbound_message(body: str) -> tuple[str | None, str]:
    """
    Returns (raw_destination_number, message_text).
    raw_destination_number is None if no explicit recipient prefix was provided.
    """
    match = EXPLICIT_DELIM_RE.match(body)
    if match:
        return match.group(1).strip(), match.group(2).strip()

    match = SPACE_DELIM_RE.match(body)
    if match:
        return match.group(1).strip(), match.group(2).strip()

    return None, body.strip()


def validate_config():
    missing = [
        name for name, val in [
            ("XMPP_JID", XMPP_JID),
            ("XMPP_PASSWORD", XMPP_PASSWORD),
            ("AA_USERNAME", AA_USERNAME),
            ("AA_PASSWORD", AA_PASSWORD),
            ("WEBHOOK_TOKEN", WEBHOOK_TOKEN),
            ("TARGET_USER_JID", TARGET_USER_JID),
        ]
        if not val
    ]
    if missing:
        logger.error(f"Missing required environment variables in secrets.env: {', '.join(missing)}")
        sys.exit(1)


class SMSBridgeBot(ClientXMPP):
    def __init__(self, jid, password):
        super().__init__(jid, password)
        # Lock down presence subscriptions so only TARGET_USER_JID can see bot status
        self.auto_authorize = False
        self.auto_subscribe = False

        self.add_event_handler("session_start", self.start)
        self.add_event_handler("message", self.handle_message)
        self.add_event_handler("presence_subscribe", self.handle_presence_subscribe)

        self.http_session: aiohttp.ClientSession | None = None
        self.recent_senders: dict[str, float] = {}
        self.sticky_timer_task: asyncio.Task | None = None
        self.pending_sms: PendingSMS | None = None
        self.sent_timestamps: deque[float] = deque()

    async def start(self, event):
        self.send_presence()
        await self.get_roster()
        logger.info("XMPP Session Started")

    def handle_presence_subscribe(self, presence):
        sender = presence['from'].bare
        if sender == TARGET_USER_JID:
            self.send_presence(pto=sender, ptype='subscribed')
            self.send_presence(pto=sender)
            logger.info(f"Approved presence subscription from {sender}")
        else:
            self.send_presence(pto=sender, ptype='unsubscribed')
            logger.warning(f"Rejected unauthorized presence subscription from {sender}")

    def _cancel_timer(self, task: asyncio.Task | None):
        if task is not None and not task.done():
            task.cancel()

    def _clear_stickiness(self):
        self.recent_senders.clear()
        self._cancel_timer(self.sticky_timer_task)
        self.sticky_timer_task = None

    def _clear_pending_sms(self):
        if self.pending_sms is not None:
            self._cancel_timer(self.pending_sms.timer_task)
            self.pending_sms = None

    def _schedule_sticky_expiry(self):
        self._cancel_timer(self.sticky_timer_task)
        try:
            loop = asyncio.get_running_loop()
            self.sticky_timer_task = loop.create_task(self._sticky_expiry_worker())
        except RuntimeError:
            self.sticky_timer_task = None

    async def _sticky_expiry_worker(self):
        try:
            await asyncio.sleep(STICKY_TIMEOUT_SECONDS)
            active = self._prune_recent_senders()
            if active:
                senders_str = ", ".join(active.keys())
                self.recent_senders.clear()
                self.sticky_timer_task = None
                self.send_message(
                    m_to=TARGET_USER_JID,
                    m_body=(
                        f"⌛ Reply session for {senders_str} expired after 15 minutes. "
                        f"Please specify a recipient number for your next message."
                    ),
                    m_type='chat',
                )
        except asyncio.CancelledError:
            pass

    async def _confirm_expiry_worker(self, pending: PendingSMS):
        try:
            await asyncio.sleep(CONFIRM_TIMEOUT_SECONDS)
            if self.pending_sms is pending:
                self.pending_sms = None
                self._clear_stickiness()
                self.send_message(
                    m_to=TARGET_USER_JID,
                    m_body=(
                        f"⏰ Confirmation timed out ({CONFIRM_TIMEOUT_SECONDS}s) — "
                        f"message to {pending.number} was not sent. "
                        f"Active reply session cleared; please specify a recipient number for your next message."
                    ),
                    m_type='chat',
                )
        except asyncio.CancelledError:
            pass

    def _prune_recent_senders(self) -> dict[str, float]:
        now = time.monotonic()
        self.recent_senders = {
            num: ts
            for num, ts in self.recent_senders.items()
            if (now - ts) < STICKY_TIMEOUT_SECONDS
        }
        return self.recent_senders

    def _check_hourly_rate_limit(self) -> bool:
        now = time.monotonic()
        while self.sent_timestamps and (now - self.sent_timestamps) >= 3600:
            self.sent_timestamps.popleft()
        return len(self.sent_timestamps) < MAX_SMS_PER_HOUR

    def record_inbound_sender(self, sender: str) -> tuple[bool, list[str], str]:
        normalized, err = normalize_uk_number(sender)
        display_sender = normalized if not err and normalized else strip_number_chars(sender)

        if err or not normalized:
            return False, [], display_sender

        active = self._prune_recent_senders()
        active[normalized] = time.monotonic()
        self._schedule_sticky_expiry()
        senders = list(active.keys())
        return len(senders) > 1, senders, display_sender

    def _is_encrypted_stanza(self, msg) -> bool:
        xml = getattr(msg, "xml", None)
        if xml is None:
            return False
        for elem in xml:
            tag = elem.tag
            if "urn:xmpp:omemo" in tag or "eu.siacs.conversations.axolotl" in tag or "jabber:x:encrypted" in tag:
                return True
        return False

    def handle_message(self, msg):
        if msg['type'] not in ('chat', 'normal'):
            return

        # Security: Only allow the configured owner JID to interact with the bot
        if msg['from'].bare != TARGET_USER_JID:
            logger.warning(f"Ignored unauthorized XMPP message from {msg['from'].bare}")
            return

        if self._is_encrypted_stanza(msg):
            msg.reply(
                "🔒 Encrypted XMPP message detected (OMEMO/PGP). "
                "Please disable encryption for this chat — standard SMS is unencrypted."
            ).send()
            return

        body = (msg['body'] or '').strip()
        if not body:
            return

        lower_body = body.lower()

        # Built-in utility commands
        if lower_body == "help":
            msg.reply(
                "📱 SMS Bridge Commands:\n"
                "• New text: 07700900000 Hello! (or +447700900000: Hello!)\n"
                "• Sticky reply: Just type your message within 15m of an incoming text\n"
                "• Confirm / Cancel: Reply y or n within 60s\n"
                "• Check session: status"
            ).send()
            return

        if lower_body == "status":
            active = self._prune_recent_senders()
            if not active:
                msg.reply("ℹ️ No active sticky reply session. Specify a UK number to send.").send()
            elif len(active) == 1:
                num, ts = next(iter(active.items()))
                mins_left = max(1, math.ceil((STICKY_TIMEOUT_SECONDS - (time.monotonic() - ts)) / 60))
                msg.reply(f"💬 Active reply session: {num} (~{mins_left}m remaining).").send()
            else:
                msg.reply(
                    f"⚠️ Multiple recent senders ({', '.join(active.keys())}). "
                    f"Sticky reply is paused; specify a recipient number."
                ).send()
            return

        # 1. Check if the user is confirming or cancelling a pending SMS
        if self.pending_sms is not None:
            pending = self.pending_sms
            is_expired = (time.monotonic() - pending.created_at) > CONFIRM_TIMEOUT_SECONDS

            if is_expired:
                self._clear_pending_sms()
                self._clear_stickiness()
                if lower_body in ("y", "yes", "n", "no", "cancel"):
                    msg.reply(
                        "Confirmation expired (60s limit) and active reply session cleared. "
                        "Please specify a recipient number for your next message."
                    ).send()
                    return
            elif lower_body in ("y", "yes"):
                self._clear_pending_sms()
                if not self._check_hourly_rate_limit():
                    msg.reply(
                        f"🛑 Hourly rate limit reached ({MAX_SMS_PER_HOUR} SMS/hour). "
                        f"Please wait before sending more messages."
                    ).send()
                    return
                if pending.number in self.recent_senders:
                    self.recent_senders[pending.number] = time.monotonic()
                    self._schedule_sticky_expiry()
                self.sent_timestamps.append(time.monotonic())
                asyncio.create_task(self.send_sms(pending.number, pending.text, msg))
                return
            elif lower_body in ("n", "no", "cancel"):
                self._clear_pending_sms()
                msg.reply("❌ Cancelled.").send()
                return
            else:
                self._clear_pending_sms()

        # 2. Parse a new outbound message (explicit prefix or sticky reply)
        raw_target, raw_text = parse_outbound_message(body)

        if raw_target is not None:
            target, err = normalize_uk_number(raw_target)
            if err:
                msg.reply(err).send()
                return
        else:
            active = self._prune_recent_senders()
            if len(active) == 0:
                msg.reply(
                    "No active conversation to reply to. "
                    "Please specify a UK recipient number (e.g., 07700900000 Hello! or +447700900000: Hello!)."
                ).send()
                return

            if len(active) > 1:
                senders_list = ", ".join(active.keys())
                msg.reply(
                    f"⚠️ Multiple recent senders ({senders_list}).\n"
                    f"Specify the UK number so your reply goes to the right person "
                    f"(e.g. {list(active.keys())[-1]} {raw_text})."
                ).send()
                return

            target = next(iter(active.keys()))

        text = normalize_smart_punctuation(raw_text).strip()
        if not text:
            msg.reply("Error: Message text cannot be empty.").send()
            return

        # 3. Bill protection: calculate SMS segments, cost, and enforce hard limits
        if not self._check_hourly_rate_limit():
            msg.reply(
                f"🛑 Hourly rate limit reached ({MAX_SMS_PER_HOUR} SMS/hour). "
                f"Message blocked to protect your balance."
            ).send()
            return

        parts, is_unicode = calculate_sms_parts(text)
        if parts > MAX_SMS_PARTS:
            msg.reply(
                f"🛑 Message too long ({parts} SMS parts; max allowed is {MAX_SMS_PARTS}). "
                f"Please shorten your message (emojis/special characters reduce each part to 67 chars)."
            ).send()
            return

        est_cost = parts * COST_PER_PART_PENCE
        encoding_note = " — Unicode/emoji detected" if is_unicode else ""
        part_label = "part" if parts == 1 else "parts"

        # 4. Stage the message, start the 60s expiry timer, and ask for active confirmation
        pending = PendingSMS(
            number=target,
            text=text,
            parts=parts,
            is_unicode=is_unicode,
            created_at=time.monotonic(),
        )
        try:
            loop = asyncio.get_running_loop()
            pending.timer_task = loop.create_task(self._confirm_expiry_worker(pending))
        except RuntimeError:
            pending.timer_task = None

        self.pending_sms = pending
        msg.reply(
            f"⚠️ About to send message to {target} ({parts} {part_label}, ~{est_cost:.1f}p{encoding_note}):\n"
            f"\"{text}\"\n\n"
            f"Reply y to confirm or n to cancel (expires in {CONFIRM_TIMEOUT_SECONDS}s)."
        ).send()

    async def send_sms(self, number: str, text: str, orig_msg):
        payload = {
            'username': AA_USERNAME,
            'password': AA_PASSWORD,
            'da': number,
            'ud': text
        }
        try:
            async with self.http_session.post(AA_API_URL, data=payload, timeout=15) as resp:
                resp_text = (await resp.text()).strip()
                # AAISP returns HTTP 200 even on API errors, prefixing body with "ERR:"
                if resp.status == 200 and not resp_text.upper().startswith("ERR"):
                    logger.info("Outbound SMS dispatched successfully")
                    orig_msg.reply(f"✓ Sent to {number}").send()
                else:
                    logger.error(f"AAISP rejected SMS (HTTP {resp.status}): {resp_text}")
                    orig_msg.reply(f"Failed to send SMS to {number}: {resp_text or f'HTTP {resp.status}'}").send()
        except Exception as e:
            logger.error(f"Network error sending SMS: {e}")
            orig_msg.reply(f"Error sending SMS to {number}: {e}").send()


async def handle_inbound_sms(request: web.Request) -> web.Response:
    token = request.query.get('token', '')
    if not WEBHOOK_TOKEN or not hmac.compare_digest(token, WEBHOOK_TOKEN):
        logger.warning("Rejected unauthorized webhook request")
        return web.Response(status=403, text="Forbidden")

    data = await request.post()
    sender = data.get('oa', 'Unknown')
    recipient = data.get('da', 'Unknown')
    message = data.get('ud', '')

    bot: SMSBridgeBot = request.app['bot']
    is_collision, active_senders, display_sender = bot.record_inbound_sender(sender)

    if is_collision:
        reply_hint = (
            f"⚠️ Multiple active senders ({', '.join(active_senders)}).\n"
            f"Include the number to reply: {display_sender} your message"
        )
    elif UK_E164_MOBILE_RE.match(display_sender):
        reply_hint = f"💬 Reply directly to send to {display_sender} (active for 15m)"
    else:
        reply_hint = "Reply with a UK mobile number (e.g., 07700900000 your message)"

    formatted_msg = (
        f"📩 Incoming SMS\n"
        f"From: {sender}\n"
        f"To: {recipient}\n\n"
        f"{message}\n\n"
        f"{reply_hint}"
    )

    bot.send_message(m_to=TARGET_USER_JID, m_body=formatted_msg, m_type='chat')
    logger.info("Forwarded inbound SMS to XMPP")

    return web.Response(text="OK")


async def main():
    validate_config()

    bot = SMSBridgeBot(XMPP_JID, XMPP_PASSWORD)
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, bot.disconnect)

    async with aiohttp.ClientSession() as session:
        bot.http_session = session

        app = web.Application()
        app['bot'] = bot
        app.router.add_post('/webhook', handle_inbound_sms)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', 8080)
        await site.start()
        logger.info("HTTP Webhook server started on port 8080")

        bot.connect()
        await bot.disconnected
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
