# Pin to specific digest for supply-chain security (python:3.13-slim as of 2025-11-29)
FROM python@sha256:326df678c20c78d465db501563f3492d17c42a4afe33a1f2bf5406a1d56b0e86 AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=on \
    PIP_DEFAULT_TIMEOUT=100 \
    PYO3_PYTHON="/usr/local/bin/python3" \
    PYSETUP_PATH="/opt/pysetup" \
    RUSTUP_TOOLCHAIN="stable" \
    BUILD_MODE="release" \
    CC="clang"
ENV PATH="/root/.local/bin:/root/.cargo/bin:$PATH"
WORKDIR $PYSETUP_PATH

FROM base AS builder
RUN apt-get update && \
    apt-get install -y curl clang git make pkg-config capnproto libcapnp-dev && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*
RUN curl https://sh.rustup.rs -sSf | bash -s -- -y
COPY uv-version ./
RUN UV_VERSION=$(cat uv-version) && curl -LsSf https://astral.sh/uv/$UV_VERSION/install.sh | sh
COPY uv.lock pyproject.toml build.py ./
RUN uv sync --no-install-package nautilus_trader
COPY Cargo.toml ./
COPY Cargo.lock ./
COPY crates ./crates
COPY nautilus_trader ./nautilus_trader
COPY deploy ./deploy
COPY README.md ./
RUN cargo build --lib --release --all-features
RUN uv build --wheel
RUN uv pip install --system dist/*.whl
RUN uv pip install --system \
    "aiohttp==3.12.14,<4.0.0" \
    "py-clob-client==0.30.0,<1.0.0"
RUN python3 - <<'PY'
import aiohttp
import nautilus_trader.adapters.polymarket  # noqa: F401
import nautilus_trader.adapters.sxbet  # noqa: F401
PY
RUN find /usr/local/lib/python3.13/site-packages -name "*.pyc" -exec rm -f {} \;

FROM base AS runtime
WORKDIR /srv/node
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin/ /usr/local/bin/
COPY --from=builder /opt/pysetup/deploy /srv/node/deploy
ENTRYPOINT ["python3", "-m", "nautilus_trader.live.strategy_nodes.betting_arbitrage"]
CMD ["--help"]
