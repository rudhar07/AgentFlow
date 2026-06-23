# Playwright's official image ships Chromium + all system libraries it needs,
# pinned to the same version as the `playwright` pip package in requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

# Hugging Face Spaces runs containers as a non-root user with UID 1000.
RUN useradd -m -u 1000 user || true
ENV HOME=/home/user \
    PYTHONUNBUFFERED=1 \
    PORT=7860

WORKDIR /app

# Install Python deps first for better layer caching.
COPY --chown=user:user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code.
COPY --chown=user:user . .

# Writable output dirs owned by the runtime user.
RUN mkdir -p screenshots logs && chown -R user:user /app

USER user
EXPOSE 7860
CMD ["uvicorn", "web.server:app", "--host", "0.0.0.0", "--port", "7860"]
