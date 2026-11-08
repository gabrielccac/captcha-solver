FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 HOME=/tmp \
    PYTHONPATH=/app/src PLAYWRIGHT_BROWSERS_PATH=/opt/pw TZ=America/Sao_Paulo
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    fonts-liberation fonts-noto-core fonts-noto-color-emoji fonts-dejavu-core fonts-open-sans \
    && curl -fsSL 'https://apt.pop-os.org/release/pool/noble/fonts/2da23d5e76bf724c49875e8fd328e1fd5a6de80e/pop-fonts_1.0.3~1629904758~24.04~2da23d5_all.deb' -o /tmp/pop-fonts.deb \
    && curl -fsSL 'https://archive.ubuntu.com/ubuntu/pool/main/f/fonts-ubuntu/fonts-ubuntu_0.869+git20240321-0ubuntu1_all.deb' -o /tmp/fonts-ubuntu.deb \
    && echo 'f5c872d750e4e663938659e7601f62e1a90b949999383c914b39303fdf24bcae  /tmp/pop-fonts.deb' | sha256sum -c - \
    && echo '52c09a231239029c0b53167d7dffd0c9d6a714d5eaf83e758fc15c9718e3b653  /tmp/fonts-ubuntu.deb' | sha256sum -c - \
    && apt-get install -y --no-install-recommends /tmp/pop-fonts.deb /tmp/fonts-ubuntu.deb \
    && rm -rf /var/lib/apt/lists/* /tmp/pop-fonts.deb /tmp/fonts-ubuntu.deb
RUN pip install playwright==1.64.1 Pillow==13.0.0 requests==2.34.2 pytest==8.3.5 \
    && python -m playwright install --with-deps webkit && chmod -R a+rX /opt/pw

WORKDIR /app
COPY experiments/gha-webkit/ experiments/gha-webkit/
RUN python experiments/gha-webkit/check_fonts.py --remove-extra && fc-cache -f
COPY src/ src/
COPY tests/ tests/
USER 1000:1000
ENTRYPOINT ["python", "-u", "experiments/gha-webkit/smoke.py"]
