FROM python:3.12-slim

LABEL maintainer="agent-sandbox"
LABEL description="Run LLM agents safely inside CI with strict capability manifests."

# Avoid interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

# Install git (needed for file listing and diff)
RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir .

# The entrypoint script that GitHub Actions will invoke
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
