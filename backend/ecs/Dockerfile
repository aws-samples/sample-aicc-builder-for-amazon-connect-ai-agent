FROM public.ecr.aws/docker/library/python:3.11-slim-bookworm

WORKDIR /app

# System dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq-dev curl && \
    rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Build-time arguments (injected by deploy.sh)
ARG ASSETS_BUCKET_NAME=""
ARG USER_POOL_ID=""
ARG USER_POOL_CLIENT_ID=""
ARG CONTACT_FLOW_KB_ID=""
ARG BRAVE_API_KEY=""

# Environment
ENV PYTHONPATH="/app:/app/src:${PYTHONPATH}"
ENV PYTHONUNBUFFERED=1
ENV S3FILES_MOUNT_PATH=/mnt/s3
ENV SESSION_STORE_BACKEND=s3files
ENV AWS_REGION=ap-northeast-2
ENV AWS_DEFAULT_REGION=ap-northeast-2
ENV ASSETS_BUCKET_NAME=${ASSETS_BUCKET_NAME}
ENV USER_POOL_ID=${USER_POOL_ID}
ENV USER_POOL_CLIENT_ID=${USER_POOL_CLIENT_ID}
ENV CONTACT_FLOW_KB_ID=${CONTACT_FLOW_KB_ID}
ENV BRAVE_API_KEY=${BRAVE_API_KEY}

# Application code
COPY . .

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "healthcheck.py"]

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
