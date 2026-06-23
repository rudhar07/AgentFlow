# Playwright's official image ships Chromium + all the system libraries it needs,
# pinned to the same version as the `playwright` pip package in requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

ENV PYTHONUNBUFFERED=1 \
    PORT=7860 \
    HOME=/tmp

WORKDIR /app

# Install Python deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code.
COPY . .

# Writable output dirs. We deliberately do NOT create a new UID-1000 user — the
# Playwright base image already owns UID 1000 (its `pwuser`), so adding one
# collides and breaks the build. chmod 777 keeps the app writable regardless of
# which UID Hugging Face runs the container as.
RUN mkdir -p screenshots logs && chmod -R 777 /app

EXPOSE 7860
CMD ["uvicorn", "web.server:app", "--host", "0.0.0.0", "--port", "7860"]
