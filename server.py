import os
import json
import asyncio
import logging
import sys
import base64
import requests
import io
import struct
from dotenv import load_dotenv




from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import uvicorn




# --------------------------------------------------
# ENV
# --------------------------------------------------
load_dotenv()




PORT = int(os.getenv("PORT", 10000))
SARVAM_API_KEY = os.getenv("SARVAM_API_KEY")




SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
MIN_CHUNK_SIZE = 3200
SPEECH_THRESHOLD = 500
SILENCE_CHUNKS = 6  # ~600ms silence ends utterance




# --------------------------------------------------
# LOGGING
# --------------------------------------------------
logging.basicConfig(
  level=logging.DEBUG,
  format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
  handlers=[logging.StreamHandler(sys.stdout)],
  force=True
)




logger = logging.getLogger("voicebot")
tts_logger = logging.getLogger("sarvam-tts")
stt_logger = logging.getLogger("sarvam-stt")




# --------------------------------------------------
# FASTAPI
# --------------------------------------------------
app = FastAPI()




# --------------------------------------------------
# FAQ
# --------------------------------------------------
FAQS = [
  (["interest", "rate"],
   "The interest rate starts from ten percent per annum and is personalized for each customer."),
  (["limit", "pre approved"],
   "Your loan limit is already sanctioned. Please check the Rupeek app."),
  (["emi", "repay"],
   "Your EMI will be auto deducted on the fifth of every month.")
]




def get_reply(text: str) -> str:
  text = text.lower()
  for keys, reply in FAQS:
      if any(k in text for k in keys):
          return reply
  return "I can help you with interest rate, loan limit, or repayment."




# --------------------------------------------------
# UTILS
# --------------------------------------------------
def is_speech(pcm: bytes) -> bool:
  total, count = 0, 0
  for i in range(0, len(pcm) - 1, 2):
      s = int.from_bytes(pcm[i:i+2], "little", signed=True)
      total += abs(s)
      count += 1
  if count == 0:
      return False
  avg = total / count
  logger.debug(f"🔈 Avg amplitude={avg}")
  return avg > SPEECH_THRESHOLD




def pcm_to_wav(pcm: bytes) -> bytes:
  buf = io.BytesIO()
  buf.write(b"RIFF")
  buf.write(struct.pack("<I", 36 + len(pcm)))
  buf.write(b"WAVEfmt ")
  buf.write(struct.pack("<IHHIIHH", 16, 1, 1, SAMPLE_RATE,
                         SAMPLE_RATE * 2, 2, 16))
  buf.write(b"data")
  buf.write(struct.pack("<I", len(pcm)))
  buf.write(pcm)
  return buf.getvalue()




# --------------------------------------------------
# SARVAM
# --------------------------------------------------
def sarvam_stt(pcm: bytes) -> str:
  wav = pcm_to_wav(pcm)
  resp = requests.post(
      "https://api.sarvam.ai/speech-to-text",
      headers={"api-subscription-key": SARVAM_API_KEY},
      files={"file": ("audio.wav", wav, "audio/wav")},
      data={"language_code": "en-IN"},
      timeout=20
  )
  stt_logger.info(f"📡 STT status={resp.status_code}")
  stt_logger.debug(f"📦 STT raw={resp.text}")
  resp.raise_for_status()




  # ✅ FIXED: transcript field
  return resp.json().get("transcript", "").strip()




def sarvam_tts(text: str) -> bytes:
  resp = requests.post(
      "https://api.sarvam.ai/text-to-speech",
      headers={
          "api-subscription-key": SARVAM_API_KEY,
          "Content-Type": "application/json"
      },
      json={
          "text": text,
          "target_language_code": "en-IN",
          "speech_sample_rate": "16000"
      },
      timeout=15
  )
  resp.raise_for_status()
  return base64.b64decode(resp.json()["audios"][0])




async def send_pcm(ws: WebSocket, pcm: bytes):
  for i in range(0, len(pcm), MIN_CHUNK_SIZE):
      await ws.send_text(json.dumps({
          "event": "media",
          "media": {
              "payload": base64.b64encode(
                  pcm[i:i + MIN_CHUNK_SIZE]
              ).decode()
          }
      }))
      await asyncio.sleep(0)




# --------------------------------------------------
# WS
# --------------------------------------------------
@app.websocket("/ws")
async def ws_handler(ws: WebSocket):
  await ws.accept()
  logger.info("🎧 Exotel connected")




  buffer = b""
  speech_buffer = b""
  silence_count = 0
  pitch_done = False




  try:
      while True:
          msg = await ws.receive()
          if "text" not in msg:
              continue




          data = json.loads(msg["text"])
          event = data.get("event")




          if event == "start" and not pitch_done:
              pcm = await asyncio.to_thread(
                  sarvam_tts,
                  "Hello, this is Rupeek personal loan assistant. "
                  "You may ask about interest rate, repayment, or loan limit."
              )
              await send_pcm(ws, pcm)
              pitch_done = True
              continue




          if event != "media":
              continue




          payload = data["media"].get("payload")
          if not payload:
              continue




          chunk = base64.b64decode(payload)
          buffer += chunk




          if len(buffer) < MIN_CHUNK_SIZE:
              continue




          frame = buffer[:MIN_CHUNK_SIZE]
          buffer = buffer[MIN_CHUNK_SIZE:]




          if is_speech(frame):
              speech_buffer += frame
              silence_count = 0
          else:
              silence_count += 1




          # ---- END OF UTTERANCE ----
          if silence_count >= SILENCE_CHUNKS and speech_buffer:
              logger.info("🗣 Utterance ended — sending to STT")
              text = await asyncio.to_thread(sarvam_stt, speech_buffer)
              speech_buffer = b""
              silence_count = 0




              if not text:
                  continue




              logger.info(f"📝 User said: {text}")
              reply = get_reply(text)
              pcm = await asyncio.to_thread(sarvam_tts, reply)
              await send_pcm(ws, pcm)




  except WebSocketDisconnect:
      logger.info("🔌 Call disconnected")
