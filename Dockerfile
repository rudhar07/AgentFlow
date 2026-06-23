# Playwright's official image ships Chromium + all the system libraries it needs,
# pinned to the same version as the `playwright` pip package in requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

ENV PYTHONUNBUFFERED=1 \
    PORT=7860

WORKDIR /app

# Install Python deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code.
COPY . .

# Run as the image's existing non-root user `pwuser`, not a freshly-created one:
#   * creating a new UID-1000 user collides with pwuser and fails the build, and
#   * Chromium refuses to run as root without --no-sandbox.
# Give pwuser ownership of the app dir so it can write screenshots/logs.
RUN mkdir -p screenshots logs && chown -R pwuser:pwuser /app
USER pwuser

EXPOSE 7860
CMD ["uvicorn", "web.server:app", "--host", "0.0.0.0", "--port", "7860"]
