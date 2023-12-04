#!/bin/bash

poetry install --with test --all-extras
poetry run pytest tests/integration_tests/adapters/cloudbet --new-first --failed-first --junitxml=report.xml
