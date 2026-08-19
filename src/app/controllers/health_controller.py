# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

from fastapi import APIRouter, Request
from app.utils.logger import logger
from app.config import Config

router = APIRouter(prefix="", tags=["Health"])

@router.get("/isalive")
async def is_alive(request: Request):
    """Health check endpoint"""
    return {
        "status": "Ok",
        "serviceName": Config.SERVICE_NAME
    }
