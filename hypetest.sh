#!/bin/bash
echo "=== TIEMPOS DE CONEXION (3 intentos) ==="
for i in 1 2 3; do
  curl -s -o /dev/null -w "Intento $i: dns=%{time_namelookup}s connect=%{time_connect}s tls=%{time_appconnect}s ttfb=%{time_starttransfer}s total=%{time_total}s http=%{http_code} bytes=%{size_download}\n" -m 20 https://redeem.hype.games/
done
echo
echo "=== HEADERS ==="
curl -sI -m 20 https://redeem.hype.games/ | head -15
echo
echo "=== HTML CONTIENE? ==="
HTML=$(curl -s -m 20 https://redeem.hype.games/)
echo "Bytes: $(echo -n "$HTML" | wc -c)"
echo "recaptcha: $(echo "$HTML" | grep -c -iE 'recaptcha|grecaptcha')"
echo "cloudflare: $(echo "$HTML" | grep -c -iE 'cloudflare|cf-ray|challenge|just a moment')"
echo
echo "=== IP PUBLICA DEL VPS ==="
curl -s -m 5 https://api.ipify.org; echo
echo
echo "=== GOOGLE RECAPTCHA ==="
curl -s -o /dev/null -w "google.com/recaptcha/api.js: ttfb=%{time_starttransfer}s http=%{http_code}\n" -m 10 https://www.google.com/recaptcha/api.js
echo
echo "=== TEST USER-AGENT REAL (como Chromium del bot) ==="
curl -s -o /dev/null -w "con UA real: ttfb=%{time_starttransfer}s total=%{time_total}s http=%{http_code} bytes=%{size_download}\n" -m 20 -A "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36" https://redeem.hype.games/
