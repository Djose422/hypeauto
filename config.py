import os
from dotenv import load_dotenv

load_dotenv()

API_SECRET_KEY = os.getenv("API_SECRET_KEY", "change-me-in-production")
PORT = int(os.getenv("PORT", "8000"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "3"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
REDEEM_TIMEOUT = int(os.getenv("REDEEM_TIMEOUT", "60"))

REDEEM_BASE_URL = "https://redeem.hype.games"

# Datos fijos para el formulario de redención (no los proporciona el cliente)
REDEEM_NAME = os.getenv("REDEEM_NAME", "Juan Perez")
REDEEM_BORN_AT = os.getenv("REDEEM_BORN_AT", "15/03/1995")
REDEEM_NATIONALITY = os.getenv("REDEEM_NATIONALITY", "CL")
