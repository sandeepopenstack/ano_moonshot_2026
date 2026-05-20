FROM python:3.12-slim

WORKDIR /app/reflection_agent
COPY . .
RUN pip install --no-cache-dir -r requirements.txt

WORKDIR /app
ENV AGENT_MODEL=gemini-2.5-flash
EXPOSE 8000

CMD ["uvicorn", "reflection_agent.server:app", "--host", "0.0.0.0", "--port", "8080"]