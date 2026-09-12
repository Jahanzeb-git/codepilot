"""
schemas.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Description: This file handles pydantic schemas for input/output validation.
Licensed: MIT
"""
# --IMPORTS--
from pydantic import BaseModel, Field, EmailStr

# --MODELS--
class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)

class MachineResponse(BaseModel):
    id: int
    fly_machine_id: str
    status: str

# --END OF FILE--