FROM python:3.12-alpine
RUN apk add --no-cache v4l-utils ffmpeg
WORKDIR /app
COPY manager.py .
EXPOSE 8971
CMD ["python", "manager.py"]
