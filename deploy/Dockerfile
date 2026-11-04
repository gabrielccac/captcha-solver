FROM python:3.12.16-slim-bookworm
ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 HOME=/tmp TZ=America/Sao_Paulo
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl xvfb fonts-liberation tzdata \
    && curl -fsSL https://dl.google.com/linux/chrome/deb/pool/main/g/google-chrome-stable/google-chrome-stable_156.0.8078.79-1_${TARGETARCH}.deb -o /tmp/chrome.deb \
    && apt-get install -y --no-install-recommends /tmp/chrome.deb \
    && rm -rf /var/lib/apt/lists/* /tmp/chrome.deb \
    && mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

# Include desktop font fallbacks used by Chrome, including emoji.
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-noto-core fonts-noto-color-emoji fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Match the working desktop font set without mounting fonts from the host.
RUN curl -fsSL 'https://apt.pop-os.org/release/pool/noble/fonts/2da23d5e76bf724c49875e8fd328e1fd5a6de80e/pop-fonts_1.0.3~1629904758~24.04~2da23d5_all.deb' -o /tmp/pop-fonts.deb \
    && curl -fsSL 'https://archive.ubuntu.com/ubuntu/pool/main/f/fonts-ubuntu/fonts-ubuntu_0.869+git20240321-0ubuntu1_all.deb' -o /tmp/fonts-ubuntu.deb \
    && echo 'f5c872d750e4e663938659e7601f62e1a90b949999383c914b39303fdf24bcae  /tmp/pop-fonts.deb' | sha256sum -c - \
    && echo '52c09a231239029c0b53167d7dffd0c9d6a714d5eaf83e758fc15c9718e3b653  /tmp/fonts-ubuntu.deb' | sha256sum -c - \
    && apt-get update && apt-get install -y --no-install-recommends fonts-open-sans /tmp/pop-fonts.deb /tmp/fonts-ubuntu.deb \
    && rm -rf /var/lib/apt/lists/* /tmp/pop-fonts.deb /tmp/fonts-ubuntu.deb

RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*


WORKDIR /app
COPY deploy/requirements-hosted.txt /app/requirements-hosted.txt
RUN pip install --no-cache-dir -r /app/requirements-hosted.txt
RUN python -c "import hashlib,pathlib; p=pathlib.Path('/usr/share/fonts'); h=hashlib.sha256(); [(h.update(str(f.relative_to(p)).encode()),h.update(f.read_bytes())) for f in sorted(p.rglob('*')) if f.is_file()]; assert h.hexdigest()=='20feac6451a727f0a0deb823f474a848f6b28cbec01882d82d00b225fb6c1891', 'Reference fonts differ'"
RUN python -c "import subprocess; v=subprocess.check_output(['google-chrome','--version'],text=True).strip(); print(v); assert v=='Google Chrome 156.0.8078.79', 'Reference Chrome differs'"
COPY src/ /app/src/
ENV PYTHONPATH=/app/src
USER 1000:1000
ENTRYPOINT ["python", "-m", "inss.hosted"]
