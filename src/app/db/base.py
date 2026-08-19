# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

from sqlalchemy.orm import declarative_base

Base = declarative_base()

# Import models so they are registered with SQLAlchemy metadata
try:
    from app.models.execution_models import Execution, LLMState  # noqa: F401
except ImportError:
    # During certain tooling (e.g., linting) the models module might not be available yet.
    Execution = None  # type: ignore
    LLMState = None  # type: ignore
