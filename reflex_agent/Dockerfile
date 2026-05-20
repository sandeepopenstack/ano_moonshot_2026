FROM python:3.12-slim

WORKDIR /app/reflex_agent
COPY . .
RUN pip install --no-cache-dir -r requirements.txt

WORKDIR /app
ENV AGENT_MODEL=gemini-2.5-flash
EXPOSE 8000

CMD ["uvicorn", "reflex_agent.server:app", "--host", "0.0.0.0", "--port", "8080"]