import uuid
from contextlib import asynccontextmanager
from typing import Dict

import httpx
from fastapi import FastAPI, HTTPException, Header, BackgroundTasks
from loguru import logger

import config
from models import RedeemRequest, RedeemResponse, RedeemStatus, ErrorType, HealthResponse
from redeemer import redeemer


# --- Estado en memoria ---
tasks: Dict[str, RedeemResponse] = {}
queue_size = 0


# --- Lifecycle ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Iniciando servidor HypeAuto...")
    await redeemer.initialize()
    yield
    logger.info("Apagando servidor...")
    await redeemer.shutdown()


app = FastAPI(
    title="HypeAuto - Redención automática de PINs",
    version="1.0.0",
    lifespan=lifespan,
)


# --- Autenticación ---
def verify_api_key(x_api_key: str = Header(...)):
    if x_api_key != config.API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="API key inválida")
    return x_api_key


# --- Webhook ---
async def send_webhook(url: str, data: dict):
    """Envía resultado al webhook de jadhstore."""
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=data)
            logger.info(f"Webhook enviado a {url}: {resp.status_code}")
    except Exception as e:
        logger.error(f"Error enviando webhook a {url}: {e}")


def _build_response(task_id: str, req: RedeemRequest, result) -> RedeemResponse:
    """Construye RedeemResponse a partir del resultado del redeemer."""
    return RedeemResponse(
        task_id=task_id,
        status=RedeemStatus.SUCCESS if result.success else RedeemStatus.FAILED,
        pin=req.pin,
        game_account_id=req.game_account_id,
        nickname=result.nickname,
        product_name=result.product_name,
        diamonds=result.diamonds,
        redeemed_at=result.redeemed_at,
        order_id=req.order_id,
        error=result.error,
        error_message=result.error_message,
        return_pin=result.return_pin,
        redeem_duration_ms=getattr(result, 'redeem_duration_ms', 0),
    )


# --- Tarea de redención en background ---
async def process_redeem(task_id: str, req: RedeemRequest):
    global queue_size
    try:
        tasks[task_id].status = RedeemStatus.PROCESSING
        queue_size = max(0, queue_size - 1)

        result = await redeemer.redeem_pin(
            pin=req.pin,
            game_account_id=req.game_account_id,
        )

        resp = _build_response(task_id, req, result)
        tasks[task_id] = resp

        # Enviar webhook
        webhook_url = req.webhook_url or config.WEBHOOK_URL
        if webhook_url:
            await send_webhook(webhook_url, resp.model_dump())

    except Exception as e:
        logger.error(f"Error procesando tarea {task_id}: {e}")
        tasks[task_id].status = RedeemStatus.FAILED
        tasks[task_id].error = ErrorType.UNKNOWN
        tasks[task_id].error_message = f"Error interno: {str(e)}"
        tasks[task_id].return_pin = True


# --- Endpoints ---

@app.get("/health", response_model=HealthResponse)
async def health():
    """Estado del servidor."""
    active = sum(1 for t in tasks.values() if t.status == RedeemStatus.PROCESSING)
    total_slots = config.BROWSER_COUNT * config.MAX_CONCURRENT
    return HealthResponse(
        status="ok",
        queue_size=queue_size,
        active_tasks=active,
        max_concurrent=total_slots,
    )


@app.post("/redeem", response_model=RedeemResponse)
async def redeem(req: RedeemRequest, background_tasks: BackgroundTasks,
                 x_api_key: str = Header(...)):
    """
    Encola una redención de PIN. Retorna un task_id para consultar el estado.
    jadhstore envía: pin + game_account_id.
    El resultado llega por webhook o se consulta con GET /task/{task_id}.
    """
    verify_api_key(x_api_key)
    global queue_size

    task_id = str(uuid.uuid4())[:8]
    queue_size += 1

    task_response = RedeemResponse(
        task_id=task_id,
        status=RedeemStatus.QUEUED,
        pin=req.pin,
        game_account_id=req.game_account_id,
        order_id=req.order_id,
    )
    tasks[task_id] = task_response

    background_tasks.add_task(process_redeem, task_id, req)

    logger.info(f"[{task_id}] Encolado PIN {req.pin[:8]}... → ID jugador: {req.game_account_id}")
    return task_response


@app.post("/redeem/sync", response_model=RedeemResponse)
async def redeem_sync(req: RedeemRequest, x_api_key: str = Header(...)):
    """
    Redención síncrona. Espera el resultado antes de responder.
    """
    verify_api_key(x_api_key)

    task_id = str(uuid.uuid4())[:8]
    logger.info(f"[{task_id}] SYNC PIN {req.pin[:8]}... → ID jugador: {req.game_account_id}")

    result = await redeemer.redeem_pin(
        pin=req.pin,
        game_account_id=req.game_account_id,
    )

    response = _build_response(task_id, req, result)

    # Webhook
    webhook_url = req.webhook_url or config.WEBHOOK_URL
    if webhook_url:
        await send_webhook(webhook_url, response.model_dump())

    return response


@app.get("/task/{task_id}", response_model=RedeemResponse)
async def get_task(task_id: str, x_api_key: str = Header(...)):
    """Consultar el estado de una redención encolada."""
    verify_api_key(x_api_key)
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Tarea no encontrada")
    return tasks[task_id]


@app.post("/redeem/batch", response_model=list[RedeemResponse])
async def redeem_batch(requests: list[RedeemRequest],
                       background_tasks: BackgroundTasks,
                       x_api_key: str = Header(...)):
    """Encola múltiples redenciones a la vez."""
    verify_api_key(x_api_key)
    global queue_size

    responses = []
    for req in requests:
        task_id = str(uuid.uuid4())[:8]
        queue_size += 1

        task_response = RedeemResponse(
            task_id=task_id,
            status=RedeemStatus.QUEUED,
            pin=req.pin,
            game_account_id=req.game_account_id,
            order_id=req.order_id,
        )
        tasks[task_id] = task_response
        background_tasks.add_task(process_redeem, task_id, req)
        responses.append(task_response)

    logger.info(f"Batch de {len(requests)} redenciones encoladas")
    return responses


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=config.PORT, reload=False)
