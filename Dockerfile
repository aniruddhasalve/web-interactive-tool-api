FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ARTIFACT_DIR=/artifacts \
    AGENT_FILE_DIR=/agent-files \
    BROWSER_PROFILE_DIR=/browser-profile

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN mkdir -p /artifacts /agent-files /browser-profile

VOLUME ["/artifacts", "/agent-files", "/browser-profile"]
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
