from pydantic import BaseModel, Field
from typing import Optional
from enum import Enum
from datetime import datetime


class RedeemStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    SUCCESS = "success"
    FAILED = "failed"


class ErrorType(str, Enum):
    NONE = ""
    INVALID_ID = "invalid_id"
    PIN_EXPIRED = "pin_expired"
    PIN_ALREADY_USED = "pin_already_used"
    PAGE_ERROR = "page_error"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


class RedeemRequest(BaseModel):
    """Petición de redención enviada desde jadhstore. Solo pin + game_account_id."""
    pin: str = Field(..., description="Código PIN de Hype (UUID) del stock de jadhstore")
    game_account_id: str = Field(..., description="ID de jugador suministrado por el cliente")
    order_id: str = Field(default="", description="ID de orden en jadhstore para tracking")
    webhook_url: str = Field(default="", description="URL webhook override (opcional)")


class RedeemResponse(BaseModel):
    """Respuesta de la API hacia jadhstore."""
    task_id: str
    status: RedeemStatus
    pin: str = ""
    game_account_id: str = ""
    nickname: str = ""
    product_name: str = ""
    diamonds: int = 0
    redeemed_at: str = ""
    order_id: str = ""
    error: ErrorType = ErrorType.NONE
    error_message: str = ""
    return_pin: bool = False
    redeem_duration_ms: int = 0


class VerifyPinRequest(BaseModel):
    """Petición para verificar si un PIN fue canjeado, sin redimirlo."""
    pin: str = Field(..., description="Código PIN a verificar")


class VerifyPinResponse(BaseModel):
    """Resultado de verificación de PIN."""
    pin_used: bool = Field(description="True si el PIN ya fue canjeado o expiró")
    pin_valid: bool = Field(description="True si el PIN es válido y no canjeado")
    detail: str = Field(default="", description="Detalle del estado (producto, error, etc)")
    error: bool = Field(default=False, description="True si hubo error al verificar")


class HealthResponse(BaseModel):
    status: str
    queue_size: int
    active_tasks: int
    max_concurrent: int
