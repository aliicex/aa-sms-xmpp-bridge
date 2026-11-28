#!/usr/bin/env python3
import asyncio
import logging
import math
import os
import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import aiohttp
from aiohttp import web
from slixmpp import ClientXMPP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("sms-xmpp-bridge")

XMPP_JID = os.getenv("XMPP_JID", "").strip()
XMPP_PASSWORD = os.getenv("XMPP_PASSWORD", "").strip()
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "").strip()
TARGET_USER_JID = os.getenv("TARGET_USER_JID", "").strip()
DEFAULT_LINE_ALIAS = os.getenv("DEFAULT_LINE", "").strip().lower()
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8080"))
AA_API_URL = "https://aa.net.uk"

ASTERISK_AMI_HOST = os.getenv("ASTERISK_AMI_HOST", "").strip()
ASTERISK_AMI_PORT = int(os.getenv("ASTERISK_AMI_PORT", "5038"))
ASTERISK_AMI_USERNAME = os.getenv("ASTERISK_AMI_USERNAME", "smsbridge").strip()
ASTERISK_AMI_PASSWORD = os.getenv("ASTERISK_AMI_PASSWORD", "").strip()


@dataclass
class LineConfig:
    alias: str
    number: str
    password: str


def load_lines_from_env() -> Dict[str, LineConfig]:
    lines: Dict[str, LineConfig] = {}
    for key, val in os.environ.items():
        if key.startswith("LINE_") and key.endswith("_NUMBER") and val.strip():
            alias = key[len("LINE_") : -len("_NUMBER")].lower()
            password = os.getenv(f"LINE_{alias.upper()}_PASSWORD", "").strip()
            number = normalize_uk_number(val.strip()) or val.strip()
            if password:
                lines[alias] = LineConfig(alias=alias, number=number, password=password)
    return lines


def normalize_uk_number(raw: str) -> Optional[str]:
    """Normalize UK mobile (+447...) to E.164 for SMS."""
    cleaned = re.sub(r"[\s\-().]", "", raw.strip())
    if cleaned.startswith("0044"):
        cleaned = "+44" + cleaned[4:]
    elif cleaned.startswith("44") and not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    elif cleaned.startswith("07"):
        cleaned = "+44" + cleaned[1:]

    if re.fullmatch(r"\+447\d{9}", cleaned):
        return cleaned
    return None


def normalize_uk_call_number(raw: str) -> Optional[str]:
    """Validate and normalize UK numbers to +44 E.164 (blocks 09 premium & international)."""
    cleaned = re.sub(r"[\s\-().]", "", raw.strip())
    if cleaned.startswith("0044"):
        cleaned = "+44" + cleaned[4:]
    elif cleaned.startswith("44") and not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    elif cleaned.startswith("0"):
        cleaned = "+44" + cleaned[1:]

    # Allow UK mobile (+447...) and UK geographic/non-geo (+441..., +442..., +443...)
    if re.fullmatch(r"\+44[1237]\d{8,9}", cleaned):
        return cleaned
    return None


def calculate_sms_parts_and_cost(text: str) -> Tuple[int, float]:
    gsm7_basic = (
        "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./"
        "0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿"
        "abcdefghijklmnopqrstuvwxyzäöñüà"
    )
    gsm7_ext = "^{}\\[~]|€"
    is_gsm7 = all(ch in gsm7_basic or ch in gsm7_ext for ch in text)

    if is_gsm7:
        length = sum(2 if ch in gsm7_ext else 1 for ch in text)
        parts = 1 if length <= 160 else math.ceil(length / 153)
    else:
        length = len(text)
        parts = 1 if length <= 70 else math.ceil(length / 67)

    cost_pence = round(parts * 3.6, 1)
    return parts, cost_pence


async def trigger_asterisk_call(destination_e164: str, line_alias: str = "") -> Tuple[bool, str]:
    """Authenticate to Asterisk AMI and originate a Jingle call to the user, bridging to AAISP."""
    if not (ASTERISK_AMI_HOST and ASTERISK_AMI_PASSWORD and TARGET_USER_JID):
        return False, "Asterisk AMI is not configured in secrets.env."

    selected_line = (line_alias or DEFAULT_LINE_ALIAS or "default").strip().lower()

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ASTERISK_AMI_HOST, ASTERISK_AMI_PORT),
            timeout=5.0,
        )
        await asyncio.wait_for(reader.readline(), timeout=3.0)

        login_cmd = (
            "Action: Login\r\n"
            f"Username: {ASTERISK_AMI_USERNAME}\r\n"
            f"Secret: {ASTERISK_AMI_PASSWORD}\r\n"
            "Events: off\r\n\r\n"
        )
        writer.write(login_cmd.encode("utf-8"))
        await writer.drain()

        login_resp = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=3.0)
        if b"Response: Success" not in login_resp:
            writer.close()
            await writer.wait_closed()
            return False, "Asterisk AMI authentication failed."

        originate_cmd = (
            "Action: Originate\r\n"
            f"Channel: Motif/xmpp_voice/{TARGET_USER_JID}\r\n"
            "Context: outbound-aaisp\r\n"
            f"Exten: {destination_e164}\r\n"
            "Priority: 1\r\n"
            f"CallerID: {destination_e164}\r\n"
            f"Variable: CALL_LINE={selected_line}\r\n"
            "Timeout: 30000\r\n"
            "Async: true\r\n\r\n"
        )
        writer.write(originate_cmd.encode("utf-8"))
        await writer.drain()

        orig_resp = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=3.0)

        writer.write(b"Action: Logoff\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

        if b"Response: Success" in orig_resp:
            return True, f"Ringing your XMPP client to connect {destination_e164} via [{selected_line}]..."
        return False, f"Asterisk rejected call: {orig_resp.decode(errors='ignore').strip()}"
    except Exception as exc:
        return False, f"Could not reach Asterisk AMI ({ASTERISK_AMI_HOST}:{ASTERISK_AMI_PORT}): {exc}"


LINES = load_lines_from_env()
if DEFAULT_LINE_ALIAS not in LINES and LINES:
    DEFAULT_LINE_ALIAS = next(iter(LINES.keys()))

@dataclass
class PendingSMS:
    line: LineConfig
    destination: str
    message: str
    parts: int
    cost_pence: float


class SMSBridgeBot(ClientXMPP):
    def __init__(self, jid: str, password: str):
        super().__init__(jid, password)
        self.pending: Optional[PendingSMS] = None
        self.last_sender: Optional[str] = None
        self.last_line: Optional[str] = None

        self.add_event_handler("session_start", self.on_session_start)
        self.add_event_handler("message", self.on_message)

    async def on_session_start(self, event) -> None:
        self.send_presence()
        await self.get_roster()
        logger.info("Connected to XMPP as %s", self.boundjid.bare)

    def send_reply(self, to_jid: str, text: str) -> None:
        self.send_message(mto=to_jid, mbody=text, mtype="chat")

    def resolve_line_for_destination(self, dest_number: str) -> Optional[LineConfig]:
        norm = normalize_uk_number(dest_number) or dest_number.strip()
        for line in LINES.values():
            if line.number == norm:
                return line
        return None

    async def send_aa_sms(self, pending: PendingSMS) -> Tuple[bool, str]:
        params = {
            "username": pending.line.number,
            "password": pending.line.password,
            "da": pending.destination,
            "ud": pending.message,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(AA_API_URL, data=params, timeout=15) as resp:
                    body = (await resp.text()).strip()
                    if resp.status == 200 and body.upper().startswith("OK"):
                        return True, f"Sent via [{pending.line.alias}] to {pending.destination} ({pending.parts} part(s), {pending.cost_pence}p)."
                    return False, f"AAISP API error ({resp.status}): {body}"
        except Exception as exc:
            return False, f"Failed to reach AAISP SMS API: {exc}"

    async def on_message(self, msg) -> None:
        if msg["type"] not in ("chat", "normal"):
            return

        sender_bare = msg["from"].bare
        if TARGET_USER_JID and sender_bare != TARGET_USER_JID:
            logger.warning("Ignoring message from unauthorized JID: %s", sender_bare)
            return

        body = (msg["body"] or "").strip()
        if not body:
            return

        lower_body = body.lower()

        # 1. Help & Status commands
        if lower_body in ("help", "?"):
            line_list = ", ".join(f"{l.alias} ({l.number})" for l in LINES.values()) or "none"
            self.send_reply(
                sender_bare,
                "Commands:\n"
                "• [line] <07...> <message> — Queue an SMS (reply YES to send)\n"
                "• <message> — Reply to last SMS or missed call\n"
                "• call [line] <UK number> — Place a voice call via Cheogram\n"
                "• call — Call back last SMS sender or missed caller\n"
                "• lines — Show configured lines\n"
                f"Configured lines: {line_list}",
            )
            return

        if lower_body == "lines":
            if not LINES:
                self.send_reply(sender_bare, "No lines configured in secrets.env.")
                return
            details = "\n".join(
                f"• {l.alias}{' (default)' if l.alias == DEFAULT_LINE_ALIAS else ''}: {l.number}"
                for l in LINES.values()
            )
            self.send_reply(sender_bare, f"Configured lines:\n{details}")
            return

        # 2. Voice Call command: "call", "call <number>", "call <line> <number>"
        if lower_body == "call" or lower_body.startswith("call "):
            parts = body.split()
            line_alias = ""
            raw_target = ""

            if len(parts) == 1:
                raw_target = self.last_sender or ""
                line_alias = self.last_line or DEFAULT_LINE_ALIAS
                if not raw_target:
                    self.send_reply(
                        sender_bare,
                        "Usage: call [line] <UK number> (or send 'call' after an SMS or missed call).",
                    )
                    return
            elif len(parts) >= 3 and parts[1].lower() in LINES:
                line_alias = parts[1].lower()
                raw_target = "".join(parts[2:])
            else:
                line_alias = DEFAULT_LINE_ALIAS
                raw_target = "".join(parts[1:])

            target_e164 = normalize_uk_call_number(raw_target)
            if not target_e164:
                self.send_reply(
                    sender_bare,
                    f"Blocked or invalid number '{raw_target}'. Only standard UK numbers (01, 02, 03, 07) are permitted.",
                )
                return

            self.last_sender = target_e164
            if line_alias:
                self.last_line = line_alias

            ok, status_msg = await trigger_asterisk_call(target_e164, line_alias)
            self.send_reply(sender_bare, status_msg)
            return

        # 3. Pending SMS Confirmation / Cancellation
        if self.pending:
            if lower_body in ("yes", "y", "send", "confirm"):
                pending = self.pending
                self.pending = None
                ok, result_msg = await self.send_aa_sms(pending)
                if ok:
                    self.last_sender = pending.destination
                    self.last_line = pending.line.alias
                self.send_reply(sender_bare, result_msg)
                return
            if lower_body in ("no", "n", "cancel", "stop"):
                self.pending = None
                self.send_reply(sender_bare, "Cancelled pending SMS.")
                return

        # 4. Parse Outbound SMS: "[line] <number> <message>" or sticky reply "<message>"
        if not LINES:
            self.send_reply(sender_bare, "No AAISP SMS lines configured in secrets.env.")
            return

        tokens = body.split(None, 2)
        selected_line: Optional[LineConfig] = None
        dest_e164: Optional[str] = None
        sms_text: str = ""

        # Check explicit line alias first: "<line> <number> <message>"
        if len(tokens) >= 3 and tokens[0].lower() in LINES:
            maybe_num = normalize_uk_number(tokens[1])
            if maybe_num:
                selected_line = LINES[tokens[0].lower()]
                dest_e164 = maybe_num
                sms_text = tokens[2]

        # Check explicit number with default line: "<number> <message>"
        if not dest_e164 and len(tokens) >= 2:
            maybe_num = normalize_uk_number(tokens[0])
            if maybe_num:
                selected_line = LINES.get(DEFAULT_LINE_ALIAS) or next(iter(LINES.values()))
                dest_e164 = maybe_num
                sms_text = body.split(None, 1)[1]

        # Fallback to sticky reply if no explicit number was supplied
        if not dest_e164:
            if not self.last_sender:
                self.send_reply(
                    sender_bare,
                    "No active conversation. Send: [line] <07...> <message> (or type 'help').",
                )
                return
            dest_e164 = normalize_uk_number(self.last_sender)
            if not dest_e164:
                self.send_reply(
                    sender_bare,
                    f"Cannot send SMS to {self.last_sender} (AAISP SMS only supports UK 07 mobile numbers).",
                )
                return
            alias = self.last_line if self.last_line in LINES else DEFAULT_LINE_ALIAS
            selected_line = LINES.get(alias) or next(iter(LINES.values()))
            sms_text = body

        parts_count, cost_pence = calculate_sms_parts_and_cost(sms_text)
        self.pending = PendingSMS(
            line=selected_line,
            destination=dest_e164,
            message=sms_text,
            parts=parts_count,
            cost_pence=cost_pence,
        )
        self.send_reply(
            sender_bare,
            f"Ready to send via [{selected_line.alias}] ({selected_line.number}) to {dest_e164}:\n"
            f"\"{sms_text}\"\n"
            f"Length: {len(sms_text)} chars ({parts_count} part(s), {cost_pence}p).\n"
            f"Reply YES to send or NO to cancel.",
        )

async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def handle_incoming_sms(request: web.Request) -> web.Response:
    token = request.query.get("token", "")
    if WEBHOOK_TOKEN and token != WEBHOOK_TOKEN:
        return web.Response(status=403, text="Forbidden")

    if request.method == "POST":
        if request.content_type == "application/json":
            payload = await request.json()
        else:
            payload = await request.post()
    else:
        payload = request.query

    sender_raw = str(payload.get("oa") or payload.get("from") or payload.get("sender") or "").strip()
    dest_raw = str(payload.get("da") or payload.get("to") or payload.get("destination") or "").strip()
    message_text = str(payload.get("ud") or payload.get("text") or payload.get("message") or "").strip()

    if not sender_raw and not message_text:
        return web.Response(status=400, text="Missing SMS payload")

    bot: SMSBridgeBot = request.app["bot"]
    sender_norm = normalize_uk_number(sender_raw) or sender_raw
    matched_line = bot.resolve_line_for_destination(dest_raw) if dest_raw else None

    if sender_norm.startswith("+"):
        bot.last_sender = sender_norm
        if matched_line:
            bot.last_line = matched_line.alias

    line_label = f" on [{matched_line.alias}]" if matched_line else ""
    if TARGET_USER_JID:
        bot.send_reply(
            TARGET_USER_JID,
            f"SMS{line_label} from {sender_norm}:\n{message_text}",
        )

    return web.Response(text="OK")


async def handle_call_event(request: web.Request) -> web.Response:
    token = request.query.get("token", "")
    if WEBHOOK_TOKEN and token != WEBHOOK_TOKEN:
        return web.Response(status=403, text="Forbidden")

    data = await request.post()
    caller_raw = str(data.get("caller", "Unknown")).strip()
    line_alias = str(data.get("line", "")).strip().lower()
    cause = str(data.get("cause", "")).strip()

    bot: SMSBridgeBot = request.app["bot"]
    normalized = normalize_uk_call_number(caller_raw) or caller_raw

    if normalized.startswith("+"):
        bot.last_sender = normalized
        if line_alias:
            bot.last_line = line_alias

    line_label = f" on [{line_alias}]" if line_alias else ""
    cause_label = f" ({cause})" if cause else ""
    alert_text = (
        f"Missed call{line_label} from {normalized}{cause_label}\n"
        f"Reply 'call' to ring back or type a message to reply by SMS."
    )

    if TARGET_USER_JID:
        bot.send_reply(TARGET_USER_JID, alert_text)

    return web.Response(text="OK")


async def main() -> None:
    if not XMPP_JID or not XMPP_PASSWORD:
        raise RuntimeError("XMPP_JID and XMPP_PASSWORD must be configured in secrets.env")

    bot = SMSBridgeBot(XMPP_JID, XMPP_PASSWORD)
    bot.connect()

    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/health", handle_health)
    app.router.add_route("*", "/sms", handle_incoming_sms)
    app.router.add_post("/call-event", handle_call_event)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    logger.info("Webhook server listening on 0.0.0.0:%s", WEBHOOK_PORT)

    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down sms-xmpp-bridge")
