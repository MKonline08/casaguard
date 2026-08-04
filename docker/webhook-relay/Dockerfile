FROM python:3.12-alpine
RUN addgroup -S casaguard && adduser -S -G casaguard casaguard
WORKDIR /app
COPY webhook-relay/relay.py /app/relay.py
USER casaguard
ENTRYPOINT ["python", "/app/relay.py"]
