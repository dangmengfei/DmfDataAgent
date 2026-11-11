FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY dmf_agent ./dmf_agent

RUN pip install --no-cache-dir -e ".[rag]"

EXPOSE 8000

CMD ["uvicorn", "dmf_agent.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
