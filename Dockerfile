# Use Python 3.11 slim (stable for telnetlib; avoid 3.13+ where telnetlib is removed)
FROM python:3.11-slim

# Install tini for proper PID 1 / signal handling
RUN apt-get update && apt-get install -y --no-install-recommends tini \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies (best layer caching)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY app.py channel_map_with_aliases.py ./

# Run as non-root user
RUN useradd -r -u 10001 appuser && chown -R appuser:appuser /app
USER appuser

# Unbuffered logging
ENV PYTHONUNBUFFERED=1

# Optional: suppress known deprecation warnings (telnetlib)
# You can remove this if you want to see warnings.
# ENV PYTHONWARNINGS=ignore:::telnetlib

ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["python","/app/app.py"]
