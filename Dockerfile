# One image for the gateway, its migrations, and the mock upstream (different commands).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first, so code changes don't invalidate this (slow) layer.
COPY pyproject.toml ./
RUN mkdir app mock_upstream && touch app/__init__.py mock_upstream/__init__.py \
    && pip install . \
    && pip uninstall -y bouncer

COPY app ./app
COPY mock_upstream ./mock_upstream
COPY migrations ./migrations
COPY alembic.ini ./
RUN pip install --no-deps .

# Never run as root inside the container.
RUN useradd --create-home --uid 10001 bouncer
USER bouncer

EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --retries=10 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz', timeout=2)"

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
