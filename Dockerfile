FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AUTO_UPDATE=false \
    DATABASE_PATH=/data/ggsel_bot.db \
    AUTORESPONDER_CONFIG_PATH=/data/autoresponder.json \
    PLUGINS_DIR=/data/plugins

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app/

WORKDIR /data
ENTRYPOINT ["python", "/app/container_entrypoint.py"]
CMD ["python", "/app/main.py"]
