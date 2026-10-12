from decimal import Decimal
from typing import Annotated, Literal
from pydantic import BaseModel, EmailStr, Field


class InvoiceRequest(BaseModel):
    client_name: str = Field(min_length=1, max_length=100)
    client_email: EmailStr
    description: str = Field(min_length=1, max_length=200)
    amount: Annotated[Decimal, Field(gt=0, max_digits=10, decimal_places=2)]
    currency: Literal["USD", "EUR", "GBP", "AUD", "CAD"] = "USD"
    due_in_days: int = Field(default=14, ge=1, le=365)