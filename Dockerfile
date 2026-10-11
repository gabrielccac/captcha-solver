FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md runner.py ./
COPY src/ src/
COPY tests/ tests/
RUN pip install .

ENTRYPOINT ["python", "runner.py"]
