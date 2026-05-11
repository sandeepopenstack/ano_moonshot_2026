FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV AGENT_MODEL=gemini-2.5-flash
ENV BIZ_METADATA_SOURCE=mock

EXPOSE 8000

CMD ["adk", "web", "engineer_agent/"]
