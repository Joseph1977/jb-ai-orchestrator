# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

from setuptools import setup, find_packages

setup(
    name="jb-ai-orchestrator-service",
    version="1.0.0",
    license="Apache-2.0",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
)
