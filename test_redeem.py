import httpx
import time
import sys

sys.stdout.reconfigure(encoding='utf-8')

start = time.time()
r = httpx.post('http://localhost:8000/redeem/sync',
    headers={'X-Api-Key': 'test-key-local', 'Content-Type': 'application/json'},
    json={
        'pin': '0271EF36-63A1-459D-95CD-63A5E55F591D',
        'game_account_id': '2643864116',
        'order_id': 'TEST-003'
    },
    timeout=120
)
elapsed = time.time() - start

data = r.json()
print(f"Tiempo: {elapsed:.2f}s")
for k, v in data.items():
    print(f"  {k}: {v}")
