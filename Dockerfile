FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

# state and output live on a mounted volume so they survive rebuilds
ENV OUT_DIR=/data/out \
    STATE_DB=/data/collector_state.db \
    LOGO_OVERRIDES_FILE=/data/logo_overrides.yaml \
    PORT=8811

EXPOSE 8811
CMD ["python3", "collector_app.py"]
