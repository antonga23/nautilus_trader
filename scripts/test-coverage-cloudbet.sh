#!/bin/bash

export PROFILE_MODE=true
poetry install --with test --all-extras
poetry run pytest tests/integration_tests/adapters/cloudbet --cov-report=term --cov-report=xml:coverage-report.xml --cov=nautilus_trader/adapters/cloudbet --new-first --failed-first
