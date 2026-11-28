import asyncio
import hmac
import logging
import math
import os
import re
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from slixmpp import ClientXMPP

# Load secrets if running manually outside systemd (systemd loads EnvironmentFile automatically)
load_dotenv('/etc/sms-xmpp-bridge/secrets.env')

XMPP_JID = os.getenv('XMPP_JID', '').strip()
XMPP_PASSWORD = os.getenv('XMPP_PASSWORD', '').strip()
WEBHOOK_TOKEN = os.getenv('WEBHOOK_TOKEN', '').strip()
TARGET_USER_JID = os.getenv("TARGET_USER_JID", "").strip()
DEFAULT_LINE_ALIAS = os.getenv("DEFAULT_LINE", "").strip().lower()
AA_API_URL = "https://aa.net.uk"

STICKY_TIMEOUT_SECONDS = 15 * 60  # 15 minutes
CONFIRM_TIMEOUT_SECONDS = 60      # 60 seconds to confirm outbound SMS
MAX_SMS_PARTS = 4                 # Hard cap per outbound SMS
MAX_SMS_PER_HOUR = 30             # Hourly outbound rate limit
COST_PER_PART_PENCE = 3.6         # AAISP standard UK outbound SMS tariff
RATE_DB_PATH = os.getenv(
    "RATE_DB_PATH",
    "/var/lib/sms-xmpp-bridge/rate-limit.sqlite3",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("SMSBridge")

# UK mobile numbers in E.164 format (+447 followed by 9 digits)
UK_E164_MOBILE_RE = re.compile(r"^\+447\d{9}$")
LINE_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,19}$")

# Matches optional line alias prefix: "<alias> <rest>", "<alias>: <rest>", or "<alias>|<rest>"
LINE_PREFIX_RE = re.compile(r"^\s*([a-zA-Z][a-zA-Z0-9_-]{0,19})\s*[:|\s]\s*(.+)$", re.DOTALL)

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


@dataclass(frozen=True)
class AALine:
    alias: str
    number: str
    username: str
    password: str
    originator: str = ""

    @property
    def display_label(self) -> str:
        return f"{self.alias} ({self.number})"


@dataclass
class ActiveSession:
    line_alias: str
    sender_number: str
    updated_at: float


@dataclass
class PendingSMS:
    line: AALine
    number: str
    text: str
    parts: int
    is_unicode: bool
    created_at: float
    timer_task: asyncio.Task | None = None


LINES_BY_ALIAS: dict[str, AALine] = {}
LINES_BY_NUMBER: dict[str, AALine] = {}
DEFAULT_LINE: AALine | None = None


def normalize_smart_punctuation(text: str) -> str:
    """Replaces mobile-keyboard curly quotes/dashes with GSM-7 equivalents."""
    return text.translate(SMART_PUNCTUATION_MAP)


def calculate_sms_parts(text: str) -> tuple[int, bool]:
    """
    Calculate SMS segment count and whether Unicode (UCS-2) encoding is triggered.

    GSM-7: 160 units for one part, 153 units per part for multipart.
    GSM-7 extended characters count as 2 units.
    UCS-2: 70 units for one part, 67 units per part for multipart.
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
        # Count UTF-16 code units: many emoji count as 2, not 1.
        unicode_units = len(text.encode("utf-16-be")) // 2
        if unicode_units <= 70:
            return 1, True
        return math.ceil(unicode_units / 67), True

    if gsm_units <= 160:
        return 1, False
    return math.ceil(gsm_units / 153), False


def strip_number_chars(raw_number: str) -> str:
    return re.sub(r"[\s\-]", "", raw_number).strip()


def normalize_uk_any_number(raw_number: str) -> str:
    """Normalizes any UK number (01/02/03/07/08) to +44... for inbound line matching."""
    cleaned = strip_number_chars(raw_number)
    if cleaned.startswith("0044"):
        return "+44" + cleaned[4:]
    if cleaned.startswith("+44"):
        return cleaned
    if cleaned.startswith("0") and len(cleaned) >= 10:
        return "+44" + cleaned[1:]
    if cleaned.startswith("7") and len(cleaned) == 10:
        return "+44" + cleaned
    return cleaned


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


def parse_outbound_message(body: str) -> tuple[AALine | None, str | None, str, str | None]:
    """
    Parses an outbound XMPP command into:
      (selected_line_or_None, raw_destination_or_None, message_text, error_or_None)

    Supported formats:
      - "<alias> <number> <msg>" / "<alias>: <number>: <msg>"
      - "<number> <msg>" / "<number>: <msg>" (uses DEFAULT_LINE)
      - "<msg>" (sticky reply: uses active session's line + recipient)
    """
    working = body.strip()
    selected_line: AALine | None = None

    # 1. Check if message starts with a configured line alias (e.g., "work 07700900000 Hi")
    prefix_match = LINE_PREFIX_RE.match(working)
    if prefix_match:
        candidate_alias = prefix_match.group(1).lower()
        if candidate_alias in LINES_BY_ALIAS:
            selected_line = LINES_BY_ALIAS[candidate_alias]
            working = prefix_match.group(2).strip()

    # 2. Extract destination number if present
    match = EXPLICIT_DELIM_RE.match(working)
    if match:
        return selected_line, match.group(1).strip(), match.group(2).strip(), None

    match = SPACE_DELIM_RE.match(working)
    if match:
        return selected_line, match.group(1).strip(), match.group(2).strip(), None

    # If user typed a line alias without a recipient number (e.g., "work Hello"), reject clearly
    if selected_line is not None:
        return (
            selected_line,
            None,
            working,
            f"Line '{selected_line.alias}' specified without a recipient number. "
            f"Use: {selected_line.alias} 07700900000 {working}",
        )

    return None, None, working, None

def validate_config():
    global DEFAULT_LINE

    missing = [
        name for name, val in [
            ("XMPP_JID", XMPP_JID),
            ("XMPP_PASSWORD", XMPP_PASSWORD),
            ("WEBHOOK_TOKEN", WEBHOOK_TOKEN),
            ("TARGET_USER_JID", TARGET_USER_JID),
        ]
        if not val
    ]
    if missing:
        logger.error(f"Missing required environment variables in secrets.env: {', '.join(missing)}")
        sys.exit(1)

    if "/" in TARGET_USER_JID or "@" not in TARGET_USER_JID:
        logger.error("TARGET_USER_JID must be a bare account JID, such as user@example.org")
        sys.exit(1)

    # Discover numbered AA_LINE_<N>_* entries sorted numerically (1, 2, 3...)
    line_indices = sorted({
        int(match.group(1))
        for key in os.environ
        if (match := re.match(r"^AA_LINE_(\d+)_(?:ALIAS|NUMBER|USERNAME|PASSWORD)$", key))
    })

    reserved_words = {"help", "status", "lines", "y", "yes", "n", "no", "cancel"}

    for idx in line_indices:
        prefix = f"AA_LINE_{idx}_"
        alias = os.getenv(f"{prefix}ALIAS", "").strip().lower()
        raw_num = os.getenv(f"{prefix}NUMBER", "").strip()
        username = os.getenv(f"{prefix}USERNAME", "").strip()
        password = os.getenv(f"{prefix}PASSWORD", "").strip()
        originator = os.getenv(f"{prefix}ORIGINATOR", "").strip()

        if not alias or not LINE_ALIAS_RE.match(alias) or alias in reserved_words:
            logger.error(f"{prefix}ALIAS has invalid or reserved value '{alias}'")
            sys.exit(1)
        if not raw_num or not username or not password:
            logger.error(f"{prefix}* ('{alias}') must define NUMBER, USERNAME, and PASSWORD")
            sys.exit(1)

        norm_num = normalize_uk_any_number(raw_num)
        if alias in LINES_BY_ALIAS:
            logger.error(f"Duplicate line alias '{alias}' in {prefix}ALIAS")
            sys.exit(1)
        if norm_num in LINES_BY_NUMBER:
            logger.error(f"Duplicate phone number '{norm_num}' in {prefix}NUMBER")
            sys.exit(1)

        line = AALine(
            alias=alias,
            number=norm_num,
            username=username,
            password=password,
            originator=originator,
        )
        LINES_BY_ALIAS[alias] = line
        LINES_BY_NUMBER[norm_num] = line

    # Backward compatibility fallback if legacy AA_USERNAME / AA_PASSWORD are used
    if not LINES_BY_ALIAS:
        legacy_user = os.getenv("AA_USERNAME", "").strip()
        legacy_pass = os.getenv("AA_PASSWORD", "").strip()
        if legacy_user and legacy_pass:
            norm_num = normalize_uk_any_number(legacy_user)
            line = AALine(
                alias="default",
                number=norm_num,
                username=legacy_user,
                password=legacy_pass,
            )
            LINES_BY_ALIAS["default"] = line
            LINES_BY_NUMBER[norm_num] = line
        else:
            logger.error("No AAISP lines configured. Define AA_LINE_1_* variables in secrets.env.")
            sys.exit(1)

    if DEFAULT_LINE_ALIAS:
        if DEFAULT_LINE_ALIAS not in LINES_BY_ALIAS:
            logger.error(f"DEFAULT_LINE '{DEFAULT_LINE_ALIAS}' not found in configured line aliases")
            sys.exit(1)
        DEFAULT_LINE = LINES_BY_ALIAS[DEFAULT_LINE_ALIAS]
    else:
        DEFAULT_LINE = next(iter(LINES_BY_ALIAS.values()))

    logger.info(
        f"Loaded {len(LINES_BY_ALIAS)} AAISP line(s): {', '.join(LINES_BY_ALIAS.keys())} "
        f"(default: {DEFAULT_LINE.alias})"
    )


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
        # Keyed by (line_alias, normalized_sender_number) -> ActiveSession
        self.recent_sessions: dict[tuple[str, str], ActiveSession] = {}
        self.sticky_timer_task: asyncio.Task | None = None
        self.pending_sms: PendingSMS | None = None

        self._init_rate_db()

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
        self.recent_sessions.clear()
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

    def _log_task_exception(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Unhandled exception in background SMS task", exc_info=exc)

    def _init_rate_db(self) -> None:
        db_dir = os.path.dirname(RATE_DB_PATH)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        with sqlite3.connect(RATE_DB_PATH, timeout=5) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sms_rate_slots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reserved_at REAL NOT NULL
                )
            """)

    def _reserve_hourly_slot(self) -> int | None:
        now = time.time()
        cutoff = now - 3600

        with sqlite3.connect(RATE_DB_PATH, timeout=5) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM sms_rate_slots WHERE reserved_at <= ?",
                (cutoff,),
            )

            row = conn.execute(
                "SELECT COUNT(*) FROM sms_rate_slots"
            ).fetchone()
            count = row[0] if row else 0

            if count >= MAX_SMS_PER_HOUR:
                return None

            cursor = conn.execute(
                "INSERT INTO sms_rate_slots (reserved_at) VALUES (?)",
                (now,),
            )
            return cursor.lastrowid

    def _finish_hourly_slot(self, reservation_id: int, sent: bool) -> None:
        with sqlite3.connect(RATE_DB_PATH, timeout=5) as conn:
            if sent:
                # Keep the slot for the rest of the rolling hour.
                return

            conn.execute(
                "DELETE FROM sms_rate_slots WHERE id = ?",
                (reservation_id,),
            )

    def _has_available_hourly_slot(self) -> bool:
        cutoff = time.time() - 3600
        with sqlite3.connect(RATE_DB_PATH, timeout=5) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM sms_rate_slots WHERE reserved_at > ?",
                (cutoff,),
            ).fetchone()
            count = row[0] if row else 0
        return count < MAX_SMS_PER_HOUR

    async def _sticky_expiry_worker(self):
        try:
            await asyncio.sleep(STICKY_TIMEOUT_SECONDS)
            if self.recent_sessions:
                sessions_str = ", ".join(
                    f"{s.sender_number} via {s.line_alias}"
                    for s in self.recent_sessions.values()
                )
                self.recent_sessions.clear()
                self.sticky_timer_task = None
                self.send_message(
                    mto=TARGET_USER_JID,
                    mbody=(
                        f"⌛ Reply session for {sessions_str} expired after 15 minutes. "
                        f"Please specify a recipient number for your next message."
                    ),
                    mtype='chat',
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
                    mto=TARGET_USER_JID,
                    mbody=(
                        f"⏰ Confirmation timed out ({CONFIRM_TIMEOUT_SECONDS}s) — "
                        f"message to {pending.number} via {pending.line.alias} was not sent. "
                        f"Active reply session cleared; please specify a recipient number for your next message."
                    ),
                    mtype='chat',
                )
        except asyncio.CancelledError:
            pass

    def _prune_recent_sessions(self) -> dict[tuple[str, str], ActiveSession]:
        now = time.monotonic()
        self.recent_sessions = {
            key: sess
            for key, sess in self.recent_sessions.items()
            if (now - sess.updated_at) < STICKY_TIMEOUT_SECONDS
        }
        return self.recent_sessions

    def resolve_inbound_line(self, raw_recipient: str) -> AALine | None:
        norm_recipient = normalize_uk_any_number(raw_recipient)
        return LINES_BY_NUMBER.get(norm_recipient)

    def record_inbound_session(
        self,
        sender: str,
        matched_line: AALine | None,
    ) -> tuple[bool, list[ActiveSession], str]:
        normalized_sender, err = normalize_uk_number(sender)
        display_sender = (
            normalized_sender if not err and normalized_sender else strip_number_chars(sender)
        )

        # Only record sticky state if both sender is a valid UK mobile AND recipient matched a known line
        if err or not normalized_sender or matched_line is None:
            active = self._prune_recent_sessions()
            return len(active) > 1, list(active.values()), display_sender

        active = self._prune_recent_sessions()
        key = (matched_line.alias, normalized_sender)
        active[key] = ActiveSession(
            line_alias=matched_line.alias,
            sender_number=normalized_sender,
            updated_at=time.monotonic(),
        )
        self._schedule_sticky_expiry()
        sessions = list(active.values())
        return len(sessions) > 1, sessions, display_sender

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
                f"• Default line ({DEFAULT_LINE.alias}): 07700900000 Hello! (or +447700900000: Hello!)\n"
                "• Specific line: <alias> 07700900000 Hello!\n"
                "• Sticky reply: Just type your message within 15m of an incoming text (uses its line)\n"
                "• Confirm / Cancel: Reply y or n within 60s\n"
                "• List lines: lines | Check session: status"
            ).send()
            return

        if lower_body == "lines":
            lines_desc = []
            for line in LINES_BY_ALIAS.values():
                default_tag = " [DEFAULT]" if line == DEFAULT_LINE else ""
                oa_tag = f" (oa: {line.originator})" if line.originator else ""
                lines_desc.append(f"• {line.alias}: {line.number}{oa_tag}{default_tag}")
            msg.reply("📞 Configured Lines:\n" + "\n".join(lines_desc)).send()
            return

        if lower_body == "status":
            active = self._prune_recent_sessions()
            if not active:
                msg.reply(
                    f"ℹ️ No active sticky reply session. "
                    f"Default outbound line: {DEFAULT_LINE.display_label}."
                ).send()
            elif len(active) == 1:
                sess = next(iter(active.values()))
                mins_left = max(1, math.ceil((STICKY_TIMEOUT_SECONDS - (time.monotonic() - sess.updated_at)) / 60))
                msg.reply(
                    f"💬 Active reply session: {sess.sender_number} via {sess.line_alias} "
                    f"(~{mins_left}m remaining)."
                ).send()
            else:
                summary = ", ".join(f"{s.sender_number} via {s.line_alias}" for s in active.values())
                msg.reply(
                    f"⚠️ Multiple recent sessions ({summary}). "
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

                reservation_id = self._reserve_hourly_slot()
                if reservation_id is None:
                    msg.reply(
                        f"🛑 Hourly rate limit reached ({MAX_SMS_PER_HOUR} SMS/hour). "
                        "Please wait before sending more messages."
                    ).send()
                    return

                session_key = (pending.line.alias, pending.number)
                if session_key in self.recent_sessions:
                    self.recent_sessions[session_key].updated_at = time.monotonic()
                    self._schedule_sticky_expiry()

                task = asyncio.create_task(
                    self.send_sms(
                        pending.line,
                        pending.number,
                        pending.text,
                        msg,
                        reservation_id=reservation_id,
                    )
                )
                task.add_done_callback(self._log_task_exception)
                return

            elif lower_body in ("n", "no", "cancel"):
                self._clear_pending_sms()
                msg.reply("❌ Cancelled.").send()
                return
            else:
                self._clear_pending_sms()

        # 2. Parse a new outbound message (explicit prefix or sticky reply)
        explicit_line, raw_target, raw_text, parse_err = parse_outbound_message(body)
        if parse_err:
            msg.reply(parse_err).send()
            return

        if raw_target is not None:
            target, err = normalize_uk_number(raw_target)
            if err:
                msg.reply(err).send()
                return
            chosen_line = explicit_line if explicit_line is not None else DEFAULT_LINE
        else:
            active = self._prune_recent_sessions()
            if len(active) == 0:
                msg.reply(
                    "No active conversation to reply to. "
                    f"Please specify a UK recipient number (e.g., 07700900000 Hello! or {DEFAULT_LINE.alias} 07700900000 Hello!)."
                ).send()
                return

            if len(active) > 1:
                last_sess = list(active.values())[-1]
                sessions_list = ", ".join(
                    f"{s.sender_number} (via {s.line_alias})" for s in active.values()
                )
                msg.reply(
                    f"⚠️ Multiple recent sessions ({sessions_list}).\n"
                    f"Specify the line and UK number so your reply goes to the right place "
                    f"(e.g. {last_sess.line_alias} {last_sess.sender_number} {raw_text})."
                ).send()
                return

            sole_session = next(iter(active.values()))
            target = sole_session.sender_number
            chosen_line = LINES_BY_ALIAS[sole_session.line_alias]

        text = normalize_smart_punctuation(raw_text).strip()
        if not text:
            msg.reply("Error: Message text cannot be empty.").send()
            return

        # 3. Bill protection: calculate SMS segments, cost, and enforce hard limits
        if not self._has_available_hourly_slot():
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
            line=chosen_line,
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
            f"⚠️ About to send message to {target} via {chosen_line.display_label} "
            f"({parts} {part_label}, ~{est_cost:.1f}p{encoding_note}):\n"
            f"\"{text}\"\n\n"
            f"Reply y to confirm or n to cancel (expires in {CONFIRM_TIMEOUT_SECONDS}s)."
        ).send()

    async def send_sms(
        self,
        line: AALine,
        number: str,
        text: str,
        orig_msg,
        reservation_id: int | None = None,
    ):
        if self.http_session is None:
            logger.error("HTTP session not initialized; cannot send SMS")
            orig_msg.reply(f"Error sending SMS to {number}: HTTP client unavailable.").send()
            if reservation_id is not None:
                self._finish_hourly_slot(reservation_id=reservation_id, sent=False)
            return

        payload = {
            'username': line.username,
            'password': line.password,
            'da': number,
            'ud': text,
        }
        if line.originator:
            payload['oa'] = line.originator

        success = False
        outcome_unknown = False

        try:
            async with self.http_session.post(AA_API_URL, data=payload, timeout=15) as resp:
                resp_text = (await resp.text()).strip()
                # AAISP returns HTTP 200 even on API errors, prefixing body with "ERR:"
                if resp.status == 200 and not resp_text.upper().startswith("ERR"):
                    success = True
                    logger.info(f"Outbound SMS dispatched successfully via line '{line.alias}'")
                    orig_msg.reply(f"✓ Sent to {number} via {line.alias}").send()
                else:
                    logger.error(f"AAISP rejected SMS via '{line.alias}' (HTTP {resp.status}): {resp_text}")
                    orig_msg.reply(
                        f"Failed to send SMS to {number} via {line.alias}: {resp_text or f'HTTP {resp.status}'}"
                    ).send()
        except Exception as e:
            outcome_unknown = True
            logger.error(f"Network error sending SMS via '{line.alias}': {e}")
            orig_msg.reply(f"Error sending SMS to {number} via {line.alias}: {e}").send()
        finally:
            if reservation_id is not None and not outcome_unknown:
                self._finish_hourly_slot(
                    reservation_id=reservation_id,
                    sent=success,
                )


async def handle_inbound_sms(request: web.Request) -> web.Response:
    token = request.query.get("token", "")
    if not WEBHOOK_TOKEN or not hmac.compare_digest(token, WEBHOOK_TOKEN):
        logger.warning("Rejected unauthorized webhook request")
        return web.Response(status=403, text="Forbidden")

    # AAISP's webhook is expected to submit form data.
    if request.content_type not in {
        "application/x-www-form-urlencoded",
        "multipart/form-data",
    }:
        return web.Response(status=415, text="Unsupported Media Type")

    try:
        data = await request.post()
    except (ValueError, web.HTTPBadRequest):
        return web.Response(status=400, text="Invalid form data")

    sender = data.get("oa")
    recipient = data.get("da")
    message = data.get("ud")

    # Require all three fields as strings, and reject implausibly large values.
    if not all(isinstance(value, str) for value in (sender, recipient, message)):
        return web.Response(status=400, text="Missing or invalid fields")

    sender = sender.strip()
    recipient = recipient.strip()

    if not sender or not recipient or not message:
        return web.Response(status=400, text="Missing or invalid fields")

    if len(sender) > 64 or len(recipient) > 64 or len(message) > 4096:
        return web.Response(status=400, text="Field too long")

    bot: SMSBridgeBot = request.app["bot"]
    matched_line = bot.resolve_inbound_line(recipient)
    is_collision, active_sessions, display_sender = bot.record_inbound_session(sender, matched_line)

    line_header = f" [{matched_line.alias}]" if matched_line else " [unmatched line]"
    recipient_display = (
        f"{recipient} ({matched_line.alias})" if matched_line else f"{recipient} (not in AA_LINE_*)"
    )

    if matched_line is None:
        reply_hint = (
            f"⚠️ Recipient {recipient} is not configured in secrets.env.\n"
            f"Specify a line & number to reply (e.g., {DEFAULT_LINE.alias} {display_sender} your message)"
        )
    elif is_collision:
        sessions_list = ", ".join(
            f"{s.sender_number} via {s.line_alias}" for s in active_sessions
        )
        reply_hint = (
            f"⚠️ Multiple active sessions ({sessions_list}).\n"
            f"Include line & number to reply: {matched_line.alias} {display_sender} your message"
        )
    elif UK_E164_MOBILE_RE.match(display_sender):
        reply_hint = (
            f"💬 Reply directly to send to {display_sender} via {matched_line.alias} (active for 15m)"
        )
    else:
        reply_hint = (
            f"Reply with a UK mobile number (e.g., {matched_line.alias} 07700900000 your message)"
        )

    formatted_msg = (
        f"📩 Incoming SMS{line_header}\n"
        f"From: {sender}\n"
        f"To: {recipient_display}\n\n"
        f"{message}\n\n"
        f"{reply_hint}"
    )

    bot.send_message(mto=TARGET_USER_JID, mbody=formatted_msg, mtype="chat")
    logger.info(
        f"Forwarded inbound SMS to XMPP (from={sender}, to={recipient}, line={matched_line.alias if matched_line else 'none'})"
    )

    return web.Response(text="OK")


async def main():
    validate_config()

    bot = SMSBridgeBot(XMPP_JID, XMPP_PASSWORD)
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, bot.disconnect)

    async with aiohttp.ClientSession() as session:
        bot.http_session = session

        app = web.Application(client_max_size=16 * 1024)
        app['bot'] = bot
        app.router.add_post('/webhook', handle_inbound_sms)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 8080)
        await site.start()
        logger.info("HTTP Webhook server started on port 8080")

        bot.connect()
        await bot.disconnected
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
