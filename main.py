import os, json, base64, asyncio, websockets
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse
from twilio.twiml.voice_response import VoiceResponse, Connect
from twilio.rest import Client
from dotenv import load_dotenv
from urllib.parse import parse_qs
from datetime import datetime
from fastapi import Query
import httpx

print(f"🔍 Websockets version in use: {websockets.__version__}")

# Load configuration
load_dotenv()
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
TWILIO_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
PORT = int(os.getenv('PORT', 5050))

if not OPENAI_API_KEY or not TWILIO_SID or not TWILIO_TOKEN:
    raise ValueError("Missing OPENAI_API_KEY or Twilio creds in .env")

twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)
call_sid_cache = {}

SYSTEM_MESSAGE = (
    "You are Eve AI, the Concierge for Absolute Health Care.\n"
    "→ Always start with exactly: \u201cHello, I’m Eve AI from Absolute Health Care—how can I help you today?\u201d\n"
    "\n"
    "**About Absolute Health Care:**\n"
    "- We provide comprehensive medical support and wellness services.\n"
    "- Office hours: Monday–Friday, 8 AM to 4 PM.\n"
    "- For medical emergencies, please hang up and dial 911 immediately.\n"
    "\n"
    "**If asked anything outside your knowledge or scope:**\n"
    "- Respond: \u201cI’m not sure about that at the moment; let me transfer you to a human specialist.\u201d\n"
    "\n"
    "**Tone & style:**\n"
    "- Keep all responses clear, concise, and professional.\n"
)

VOICE = 'alloy'
LOG_EVENT_TYPES = [
    'response.content.done', 'rate_limits.updated', 'response.done',
    'input_audio_buffer.committed', 'input_audio_buffer.speech_stopped',
    'input_audio_buffer.speech_started', 'session.created',
    'response.text.delta'
]

app = FastAPI()

@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "AI Concierge is running"}

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid")
    call_sid_cache["last"] = call_sid
    print(f"📦 Cached call_sid: {call_sid}")

    response = VoiceResponse()
    response.say("Thank you for calling Absolute Healthcare - How may I help you?.")
    connect = Connect()
    connect.stream(url=f"wss://twilliocallingapplication.onrender.com/media-stream?callSid={call_sid}")
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    await websocket.accept()
    query_string = websocket.scope["query_string"].decode()
    query = parse_qs(query_string)
    call_sid = query.get("callSid", [None])[0] or call_sid_cache.get("last")
    print(f"📞 Final call_sid used: {call_sid if call_sid else '[MISSING]'}")
    stream_sid = None
    full_text = ""

    async with websockets.connect(
        'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01',
        extra_headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "OpenAI-Beta": "realtime=v1"
        }
    ) as openai_ws:

        await send_session_update(openai_ws)

        async def recv_twilio():
            nonlocal stream_sid
            last_audio_time = datetime.utcnow()
            prompt_sent = False

            async def silence_watchdog():
                nonlocal prompt_sent
                while not prompt_sent:
                    await asyncio.sleep(1)
                    if (datetime.utcnow() - last_audio_time).total_seconds() > 3:
                        print("⏰ No speech detected. Sending prompt...")
                        await openai_ws.send(json.dumps({
                            "type": "input_text",
                            "text": "I cannot hear you. Did you say something? How can I help you?"
                        }))
                        prompt_sent = True

            asyncio.create_task(silence_watchdog())

            async for msg in websocket.iter_text():
                data = json.loads(msg)
                if data.get("event") == "start":
                    stream_sid = data['start']['streamSid']
                elif data.get("event") == "media":
                    last_audio_time = datetime.utcnow()
                    await openai_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": data['media']['payload']
                    }))
                elif data.get("event") == "input_audio_buffer.speech_started":
                    last_audio_time = datetime.utcnow()

        async def send_twilio():
            nonlocal full_text
            GOODBYE_TRIGGERS = [
                "thank you bye have a great day", "thank you and goodbye",
                "goodbye have a nice day", "thank you have a great day",
                "have a great day", "talk to you later", "take care", "goodbye"
            ]

            def is_goodbye_trigger(text):
                return any(trigger in text.lower().strip() for trigger in GOODBYE_TRIGGERS)

            current_response = ""
            goodbye_detected = False

            while True:
                data = json.loads(await openai_ws.recv())
                if data.get("type") == "response.text.delta":
                    delta = data.get("delta", "")
                    print(f"🤖 AI (delta): {delta.strip()}")
                    current_response += delta.lower()
                    full_text += delta.lower()

                elif data.get("type") == "response.done":
                    print("📘 Assistant finished speaking.")
                    if goodbye_detected:
                        print("🚩 Hanging up due to goodbye.")
                        if call_sid:
                            try:
                                call = twilio_client.calls(call_sid).fetch()
                                if call.status == "in-progress":
                                    twilio_client.calls(call_sid).update(
                                        twiml='<Response><Pause length="4"/><Hangup/></Response>'
                                    )
                                else:
                                    twilio_client.calls(call_sid).update(status="completed")
                            except Exception as e:
                                print(f"❌ Hangup error: {e}")
                        return
                    current_response = ""

                elif data.get("type") == "response.audio.delta" and data.get("delta"):
                    try:
                        decoded = base64.b64decode(data["delta"])
                        payload = base64.b64encode(decoded).decode("ascii")
                        await websocket.send_json({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": payload}
                        })
                    except Exception as e:
                        print(f"❌ Audio send error: {e}")

                if data.get("type") == "response.done":
                    try:
                        content_items = data.get("response", {}).get("output", [])[0].get("content", [])
                        for item in content_items:
                            if item.get("type") == "audio" and "transcript" in item:
                                transcript = item["transcript"].lower()
                                print(f"📝 Transcript: {transcript}")
                                if is_goodbye_trigger(transcript):
                                    print("🚩 Goodbye detected. Awaiting response completion.")
                                    goodbye_detected = True
                    except Exception as e:
                        print(f"⚠️ Transcript parse error: {e}")

                if data.get("type") in LOG_EVENT_TYPES:
                    print("📡 Event:", data["type"])

        await asyncio.gather(recv_twilio(), send_twilio())


async def send_session_update(openai_ws):
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad"},
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": VOICE,
            "instructions": SYSTEM_MESSAGE,
            "modalities": ["audio", "text"],
            "temperature": 0.8
        }
    }
    await openai_ws.send(json.dumps(session_update))

@app.get("/oauth/callback")
async def oauth_callback(code: str = Query(...), state: str = Query(None)):
    print(f"🔐 Received authorization code: {code}")
    
    token_url = "https://oauthserver.eclinicalworks.com/oauth/oauth2/token"
    client_id = os.getenv("HEALOW_CLIENT_ID")
    client_secret = os.getenv("HEALOW_CLIENT_SECRET")
    redirect_uri = "https://twilliocallingapplication.onrender.com/oauth/callback"
    
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(token_url, data=data)
        print(f"🎟️ Token Response: {response.status_code}")
        print(f"📦 Response JSON: {response.text}")
        return response.json()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
