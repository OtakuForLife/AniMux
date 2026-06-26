FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends mkvtoolnix \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/ backend/
COPY frontend/ frontend/

RUN pip install --no-cache-dir -r backend/requirements.txt

ENV SOURCE_DIR=/source \
    DEST_DIR=/destination

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
