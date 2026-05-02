from pyrogram import Client
from dotenv import load_dotenv
import os

load_dotenv()
api_id = int(os.getenv("TG_API_ID"))
api_hash = os.getenv("TG_API_HASH")

app = Client(os.getenv("TG_SESSION", "mrkt_session"), api_id=api_id, api_hash=api_hash)
app.run()