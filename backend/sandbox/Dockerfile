# JARVIS sandbox image: disposable Linux workspace for shell, Python, a headless-capable Chromium and a virtual desktop.
# Runs as an unprivileged user on an --internal Docker network; its only way out is the egress proxy (allowlist).
FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1 PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb xdotool imagemagick fluxbox fonts-dejavu-core ca-certificates curl git jq unzip procps \
    && rm -rf /var/lib/apt/lists/*
RUN pip install playwright==1.55.0 pandas numpy matplotlib requests beautifulsoup4 \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 sandbox && mkdir -p /workspace && chown sandbox /workspace
COPY start.sh browse.py egress_proxy.py /opt/jarvis/
RUN chmod 755 /opt/jarvis/*.sh
USER sandbox
WORKDIR /workspace
ENV HOME=/tmp/home DISPLAY=:99
CMD ["/opt/jarvis/start.sh"]
