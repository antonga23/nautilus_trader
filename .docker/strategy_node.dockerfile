# Pin to linux/amd64 python:3.12-slim for compatibility with the CI-built wheel.
FROM python@sha256:4386a385d81dba9f72ed72a6fe4237755d7f5440c84b417650f38336bbc43117 AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=on
WORKDIR /srv/node

COPY dist/*.whl /tmp/wheels/
RUN wheel_path="$(find /tmp/wheels -maxdepth 1 -name 'nautilus_trader-*.whl' | sort | tail -n 1)" && \
    test -n "$wheel_path" && \
    python3 -m pip install --no-cache-dir "${wheel_path}[polymarket]" "aiohttp==3.12.14,<4.0.0" && \
    rm -rf /tmp/wheels
COPY deploy /srv/node/deploy
RUN python3 - <<'PY'
import aiohttp
import nautilus_trader.adapters.polymarket  # noqa: F401
import nautilus_trader.adapters.sxbet  # noqa: F401
PY
RUN find /usr/local/lib/python3.12/site-packages -name "*.pyc" -exec rm -f {} \;
ENTRYPOINT ["python3", "-m", "nautilus_trader.live.strategy_nodes.betting_arbitrage"]
CMD ["--help"]
