# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from app.controllers import (
    health_controller,
    agent_controller,
    ag_ui_controller,
    orchestrator_controller,
)
from app.config import Config
from app.db.session import init_db
from app.services.binding_contract import (
    sanitize_validation_errors,
    stable_binding_validation_error,
)
from app.utils.logger import logger, initialize_logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    try:
        initialize_logger()
        Config.validate_config()
        logger.info(f"Starting {Config.SERVICE_NAME} in {Config.ENVIRONMENT} environment")
        logger.info(f"Configured {len(Config.MCP_SERVER_URLS)} MCP servers:")
        for i, url in enumerate(Config.MCP_SERVER_URLS):
            logger.info(f"  - mcp{i+1}: {url}")
        logger.info(f"LiteLLM Server URL: {Config.LITELLM_BASE_URL}")
        
        # Initialize MCP service after configuration is loaded
        await init_db()
        from app.controllers.agent_controller import initialize_mcp_service
        initialize_mcp_service()
        logger.info("MCP Agent Service initialized successfully")
    except Exception as e:
        logger.error(f"Failed to start service: {str(e)}")
        raise
    
    yield
    
    # Shutdown (if needed)
    logger.info(f"Shutting down {Config.SERVICE_NAME}")


app = FastAPI(
    title=Config.SERVICE_NAME,
    description="AI Agent service with MCP tools integration",
    version="1.0.0",
    # Use /swagger instead of /docs
    docs_url="/swagger",
    # Disable ReDoc as we only want Swagger
    redoc_url=None,
    root_path=Config.SWAGGER_BASE_PATH,
    lifespan=lifespan
)


@app.exception_handler(RequestValidationError)
async def sanitized_request_validation_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return validation failures without echoing credential-bearing input."""
    errors = exc.errors()
    stable = stable_binding_validation_error(
        exc.body,
        errors,
        gate_output=request.url.path in {
            "/v1/orchestrator/initiate",
            "/api/ag-ui/run",
        },
    )
    if stable is not None:
        return JSONResponse(status_code=400, content=stable)
    return JSONResponse(
        status_code=422,
        content={"detail": sanitize_validation_errors(errors)},
    )


# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(health_controller.router)
app.include_router(agent_controller.router, prefix="/v1")
app.include_router(orchestrator_controller.router, prefix="/v1")
app.include_router(ag_ui_controller.router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
