# HypeAuto - Redención Automática de PINs Hype Games

API para redimir automáticamente PINs de [redeem.hype.games](https://redeem.hype.games) usando Playwright.
Recibe órdenes desde **jadhstore** (Render) y ejecuta las redenciones (~11s por PIN).

## Arquitectura

```
jadhstore (Render) ---[pin + game_account_id]---> HypeAuto API (VPS) ---> redeem.hype.games
                   <--[nickname, diamonds, status]--- webhook <---
```

## Flujo

1. jadhstore envía `pin` (del stock) + `game_account_id` (del cliente)
2. HypeAuto abre el navegador, valida el PIN, llena el formulario, verifica el ID, canjea
3. Devuelve: `nickname`, `product_name`, `diamonds`, `status`, `return_pin`
4. Si falla (ID incorrecto, error de página): `return_pin: true` → jadhstore devuelve el PIN al stock

## Instalación Local

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # editar con tus valores
python server.py
```

## Deploy en VPS (Docker)

```bash
git clone <tu-repo> hypeauto && cd hypeauto
cp .env.example .env
nano .env
docker compose up -d --build
docker compose logs -f
```

## API

Todos los endpoints (excepto `/health`) requieren header `X-Api-Key`.

### `GET /health`
```json
{ "status": "ok", "queue_size": 0, "active_tasks": 0, "max_concurrent": 3 }
```

### `POST /redeem` (Asíncrono - Recomendado)

jadhstore envía solo **pin + game_account_id**:

```bash
curl -X POST http://tu-vps:8000/redeem \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: tu-clave-secreta" \
  -d '{
    "pin": "9902A509-250D-4AA8-80F6-D609C28C2951",
    "game_account_id": "2643864116",
    "order_id": "ORD-001"
  }'
```

Respuesta inmediata:
```json
{
  "task_id": "a1b2c3d4",
  "status": "queued",
  "pin": "9902A509-...",
  "game_account_id": "2643864116",
  "order_id": "ORD-001"
}
```

Resultado (vía webhook o `GET /task/{task_id}`):
```json
{
  "task_id": "a1b2c3d4",
  "status": "success",
  "pin": "9902A509-...",
  "game_account_id": "2643864116",
  "nickname": "ㅤㅤﾠzєяσﾠтωσ",
  "product_name": "Free Fire - 100 Diamantes + 10% de Bonus - Chile",
  "diamonds": 100,
  "redeemed_at": "2026-02-27T00:49:20.372875+00:00",
  "order_id": "ORD-001",
  "error": "",
  "error_message": "",
  "return_pin": false
}
```

### `POST /redeem/sync`
Igual pero espera el resultado (~11-15s).

### `POST /redeem/batch`
Array de `{ pin, game_account_id, order_id }`. Encola todas.

### `GET /task/{task_id}`
Consultar estado de una redención.

## Manejo de errores y `return_pin`

| Error | `return_pin` | Significado |
|-------|-------------|-------------|
| `invalid_id` | `true` | ID de jugador incorrecto. PIN no consumido → devolver al stock |
| `page_error` | `true` | Problema técnico. PIN no consumido → devolver al stock |
| `timeout` | `true` | Timeout. PIN no consumido → devolver al stock |
| `pin_expired` | `false` | PIN expirado. No sirve |
| `pin_already_used` | `false` | PIN ya canjeado |
| `unknown` | `false` | PIN posiblemente consumido |

## Montos de diamantes

| Diamantes | Precio |
|-----------|--------|
| 110 | $0.87 |
| 341 | $2.76 |
| 572 | $4.04 |
| 1166 | $7.55 |
| 2376 | $14.95 |
| 6138 | $37.84 |

Los precios se manejan en jadhstore. HypeAuto solo redime y devuelve la cantidad de diamantes.

## Variables de Entorno

| Variable | Descripción | Default |
|----------|-------------|---------|
| `API_SECRET_KEY` | Clave para autenticar peticiones | `change-me-in-production` |
| `PORT` | Puerto del servidor | `8000` |
| `MAX_CONCURRENT` | Navegadores simultáneos | `3` |
| `WEBHOOK_URL` | URL webhook global | `` |
| `HEADLESS` | Modo headless (`true` en VPS) | `true` |
| `REDEEM_TIMEOUT` | Timeout por redención (seg) | `60` |
| `REDEEM_NAME` | Nombre fijo para formulario | `Juan Perez` |
| `REDEEM_BORN_AT` | Fecha nacimiento fija (dd/mm/yyyy) | `15/03/1995` |
| `REDEEM_NATIONALITY` | Nacionalidad fija (ISO) | `CL` |

## Integración con jadhstore

```javascript
// Cuando el cliente paga en jadhstore
const res = await fetch('http://tu-vps:8000/redeem', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'X-Api-Key': process.env.HYPEAUTO_API_KEY
  },
  body: JSON.stringify({
    pin: order.hypePin,                // del stock
    game_account_id: order.playerGameId, // del cliente
    order_id: order.id,
    webhook_url: 'https://jadhstore.onrender.com/api/hype-callback'
  })
});

// Webhook recibe el resultado automáticamente
// Si response.return_pin === true → devolver PIN al stock
```
