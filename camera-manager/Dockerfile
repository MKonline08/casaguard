FROM python:3.12-alpine
RUN apk add --no-cache v4l-utils ffmpeg
RUN ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libx264
WORKDIR /app
COPY manager.py .
COPY test_manager.py .
COPY go2rtc-smoke.yaml .
COPY run-go2rtc-smoke.sh .
RUN python -m unittest -v test_manager.py
RUN sh /app/run-go2rtc-smoke.sh && rm -f /app/go2rtc-smoke.yaml /app/run-go2rtc-smoke.sh
EXPOSE 8971
CMD ["python", "manager.py"]
