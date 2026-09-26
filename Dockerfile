# Switchboard: the local board for AI coding agents (FSL-1.1-ALv2).
#   Try the demo:  docker run --rm -p 47834:47834 ghcr.io/willykeenan/switchboard --demo --host 0.0.0.0
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir . && useradd --create-home board && mkdir -p /data && chown board /data
USER board
ENV SWITCHBOARD_HOME=/data
VOLUME ["/data"]
EXPOSE 47834
ENTRYPOINT ["switchboard"]
CMD ["--host", "0.0.0.0", "--data-dir", "/data"]
