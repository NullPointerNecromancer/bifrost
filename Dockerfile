FROM debian:trixie-slim

# fdroidserver drar med sig apksigner och Java (för keytool)
RUN apt-get update \
 && apt-get install -y --no-install-recommends fdroidserver python3-qrcode ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY bifrost.py icon.png /app/

ENV BIFROST_DATA=/data \
    PORT=8080 \
    PYTHONUNBUFFERED=1

VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=1m --timeout=5s --start-period=2m \
  CMD python3 -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/', timeout=4)" || exit 1

CMD ["python3", "/app/bifrost.py"]
