import asyncio
import os
import logging
from aiohttp import web
from slixmpp import ClientXMPP
from dotenv import load_dotenv

# Load secrets just-in-time from the environment file
load_dotenv('/etc/sms-xmpp-bridge/secrets.env')

# Configuration
XMPP_JID = os.getenv('XMPP_JID')
XMPP_PASSWORD = os.getenv('XMPP_PASSWORD')
AA_USERNAME = os.getenv('AA_USERNAME')
AA_PASSWORD = os.getenv('AA_PASSWORD')
AA_API_URL = "https://sms.aa.net.uk/sms.cgi"
WEBHOOK_TOKEN = os.getenv('WEBHOOK_TOKEN')

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SMSBridge")

class SMSBridgeBot(ClientXMPP):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_event_handler("session_start", self.start)
        self.add_event_handler("message", self.handle_message)

    async def start(self, event):
        self.send_presence()
        logger.info("XMPP Session Started")

    def handle_message(self, msg):
        if msg['type'] in ('chat', 'normal'):
            body = msg['body']
            # Syntax for sending: "number|message"
            if '|' in body:
                try:
                    target, text = body.split('|', 1)
                    asyncio.create_task(self.send_sms(target.strip(), text.strip()))
                except Exception as e:
                    msg.reply(f"Error: Use format 'number|message'. {e}").send()
            else:
                msg.reply("To send SMS, use format: +44123456789|Your message").send()

    async def send_sms(self, number, text):
        import aiohttp
        payload = {
            'username': AA_USERNAME,
            'password': AA_PASSWORD,
            'da': number,
            'ud': text
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(AA_API_URL, data=payload) as resp:
                if resp.status == 200:
                    logger.info(f"SMS sent to {number}")
                else:
                    logger.error(f"Failed to send SMS: {resp.status}")

# Inbound SMS Webhook Handler
async def handle_inbound_sms(request):
    token = request.query.get('token')
    if token != WEBHOOK_TOKEN:
        return web.Response(status=403)

    data = await request.post()
    # Fields from AA API: oa (originator), da (destination), ud (user data/message)
    sender = data.get('oa')
    recipient = data.get('da')
    message = data.get('ud')

    formatted_msg = f"📩 Incoming SMS\nFrom: {sender}\nTo: {recipient}\n\n{message}\n\nReply with: {sender}|your message"

    # Send to the XMPP user (Configure target JID in secrets.env)
    target_jid = os.getenv('TARGET_USER_JID')
    bot.send_message(m_to=target_jid, m_body=formatted_msg, m_type='chat')

    return web.Response(text="OK")

async def main():
    global bot
    bot = SMSBridgeBot(XMPP_JID, XMPP_PASSWORD)

    # Start XMPP bot in background
    bot_task = asyncio.create_task(bot.connect())

    # Start HTTP Server for AA Webhooks
    app = web.Application()
    app.router.add_post('/webhook', handle_inbound_sms)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()

    logger.info("HTTP Webhook server started on port 8080")
    await asyncio.gather(bot_task)

if __name__ == "__main__":
    asyncio.run(main())
