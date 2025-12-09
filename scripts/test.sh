#!/bin/bash

uv sync --all-groups --all-extras
uv run --no-sync pytest --ignore=tests/performance_tests --new-first --failed-first

# LEGACY build method
# poetry install --with test --all-extras
# poetry run pytest --ignore=tests/performance_tests --new-first --failed-first --junitxml=report.xml