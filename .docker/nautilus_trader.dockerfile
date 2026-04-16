# Build the runner bootstrap target:
# docker build -f .docker/nautilus_trader.dockerfile --target runner-bootstrap -t nautilus-runner-bootstrap .
#
# The runner-bootstrap target codifies the self-hosted runner host prerequisites:
# system packages, docker access, runner user creation, passwordless sudo,
# pinned GitHub Actions runner install, and a systemd service template.
#
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

FROM base AS runner-bootstrap
ENV DEBIAN_FRONTEND=noninteractive \
    RUNNER_USER=actions-runner \
    RUNNER_GROUP=actions-runner \
    RUNNER_HOME=/home/actions-runner \
    RUNNER_ROOT=/opt/actions-runner \
    PACKAGES_FILE=/usr/local/share/cloudbet/self-hosted-runner-packages.txt \
    RUNNER_SERVICE_TEMPLATE=/usr/local/share/cloudbet/github-actions-runner.service.tmpl \
    RUNNER_CHECKSUMS_FILE=/usr/local/share/cloudbet/github-actions-runner-sha256sums.txt

COPY .docker/self-hosted-runner-packages.txt $PACKAGES_FILE
COPY .docker/bootstrap-self-hosted-runner-host.sh /usr/local/bin/bootstrap-self-hosted-runner-host
COPY .docker/install-github-actions-runner.sh /usr/local/bin/install-github-actions-runner
COPY .docker/repair-github-runner-workspace.sh /usr/local/bin/repair-github-runner-workspace
COPY .docker/github-actions-runner.service.tmpl $RUNNER_SERVICE_TEMPLATE
COPY .docker/github-actions-runner-sha256sums.txt $RUNNER_CHECKSUMS_FILE

RUN chmod +x /usr/local/bin/bootstrap-self-hosted-runner-host /usr/local/bin/install-github-actions-runner /usr/local/bin/repair-github-runner-workspace && \
    BOOTSTRAP_MODE=image /usr/local/bin/bootstrap-self-hosted-runner-host

FROM base AS builder

# Install build deps
RUN apt-get update && \
    apt-get install -y curl clang git make pkg-config capnproto libcapnp-dev && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install Rust
RUN curl https://sh.rustup.rs -sSf | bash -s -- -y

# Install UV
COPY uv-version ./
RUN UV_VERSION=$(cat uv-version) && curl -LsSf https://astral.sh/uv/$UV_VERSION/install.sh | sh

# Install package requirements
COPY uv.lock pyproject.toml build.py ./
RUN uv sync --no-install-package nautilus_trader

# Build nautilus_trader
COPY Cargo.toml ./
COPY Cargo.lock ./
COPY crates ./crates
RUN cargo build --lib --release --all-features

COPY nautilus_trader ./nautilus_trader
COPY README.md ./
RUN uv build --wheel
RUN uv pip install --system dist/*.whl
RUN find /usr/local/lib/python3.13/site-packages -name "*.pyc" -exec rm -f {} \;

# Final application image
FROM base AS application

COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin/ /usr/local/bin/
