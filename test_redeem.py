import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import requests
import time

# Cambiar a VPS o localhost
BASE = "http://74.208.193.132:8000"
API_KEY = "hype-jadh422-2026-secretkey"

PIN = "F44E75C2-90A3-4116-855D-A6C0C7CF1A55"
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
