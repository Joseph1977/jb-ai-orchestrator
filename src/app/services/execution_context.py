# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ExecutionMode(str, Enum):
    API = "api"
    AGUI = "agui"


@dataclass
class ExecutionContext:
    execution_id: uuid.UUID
    mode: ExecutionMode
    thread_id: Optional[str] = None
    run_id: Optional[str] = None
