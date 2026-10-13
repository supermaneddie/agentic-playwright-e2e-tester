FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 CONFIG_FILE=/app/config.yml PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
WORKDIR /app
COPY pyproject.toml requirements.txt ./
COPY app ./app
RUN python -m pip install --no-cache-dir .
# Install Chromium AND Linux system libraries at build time, before dropping root.
RUN python -m playwright install --with-deps chromium \
    && chmod -R a+rX /opt/playwright
RUN useradd --create-home --uid 10001 assistant \
    && mkdir /app/data && chown assistant:assistant /app/data
COPY config.yml ./config.yml
USER assistant
EXPOSE 8091
VOLUME ["/app/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request; from app.config import load_config; urllib.request.urlopen('http://127.0.0.1:%s/health' % load_config().server.port, timeout=3)" || exit 1
CMD ["python", "-m", "app"]
