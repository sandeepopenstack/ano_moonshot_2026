FROM python:3.12-slim

WORKDIR /app/engineer_agent
COPY . .
RUN pip install --no-cache-dir -r requirements.txt

WORKDIR /app
ENV AGENT_MODEL=gemini-2.5-flash
ENV BIZ_METADATA_SOURCE=mock
EXPOSE 8080

CMD ["uvicorn", "engineer_agent.server:app", "--host", "0.0.0.0", "--port", "8080"]