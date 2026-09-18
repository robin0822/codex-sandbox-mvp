# syntax=docker/dockerfile:1.7

FROM rust:1.95-bookworm AS builder

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    build-essential \
    pkg-config \
    libcap-dev \
    clang \
    cmake \
    protobuf-compiler \
    patch \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /source
COPY vendor/codex/codex-rs/ /source/codex-rs/
COPY runner/patches/skill-loaded.patch /tmp/skill-loaded.patch
WORKDIR /source/codex-rs
RUN patch -p1 < /tmp/skill-loaded.patch

RUN cargo build \
    --locked \
    --release \
    -p codex-cli \
    -p codex-linux-sandbox

FROM scratch AS codex-binary
COPY --from=builder /source/codex-rs/target/release/codex /codex


FROM arm64v8/ubuntu:24.04

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    ca-certificates \
    curl \
    git \
    jq \
    ripgrep \
    python3 \
    python3-pip \
    nodejs \
    npm \
    build-essential \
    bubblewrap \
    libcap2 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /source/codex-rs/target/release/codex /usr/local/bin/codex
COPY --from=builder /source/codex-rs/target/release/codex-linux-sandbox /usr/local/bin/codex-linux-sandbox

COPY runner/config.toml /opt/codex/config.toml
COPY runner/runner-wait /usr/local/bin/runner-wait
COPY runner/run-codex-task /usr/local/bin/run-codex-task
COPY runner/codex-exec-stream.py /usr/local/bin/codex-exec-stream.py

RUN chmod +x \
      /usr/local/bin/codex \
      /usr/local/bin/codex-linux-sandbox \
      /usr/local/bin/runner-wait \
      /usr/local/bin/run-codex-task \
    && useradd --create-home --uid 10001 codex \
    && mkdir -p /job /workspace /results /home/codex/.codex /home/codex/.agents/skills \
    && cp /opt/codex/config.toml /home/codex/.codex/config.toml \
    && chown -R codex:codex /job /workspace /results /home/codex

USER codex
WORKDIR /workspace
ENV CODEX_HOME=/home/codex/.codex

ENTRYPOINT ["/usr/local/bin/runner-wait"]
