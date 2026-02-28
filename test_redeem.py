import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import requests
import time

# Cambiar a VPS o localhost
BASE = "http://74.208.193.132:8000"
API_KEY = "hype-jadh422-2026-secretkey"

PIN = "A680D02B-8E62-451C-91E7-2B030D007190"
GAME_ID = "2643864116"

print(f"Probando PIN: {PIN[:8]}... en {BASE}")
t = time.time()
r = requests.post(
    f"{BASE}/redeem/sync",
    json={"pin": PIN, "game_account_id": GAME_ID},
    headers={"X-Api-Key": API_KEY},
    timeout=60,
)
elapsed = time.time() - t
data = r.json()
print(f"Status: {r.status_code}")
print(f"Tiempo total (incluye red): {elapsed:.2f}s")
for k, v in data.items():
    print(f"  {k}: {v}")
