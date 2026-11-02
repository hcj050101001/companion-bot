FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
RUN uv sync --locked --no-dev --no-install-project
ENV PATH="/app/.venv/bin:${PATH}"
COPY . .
RUN mkdir -p data logs \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/health/ready', timeout=3)"
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8090"]
