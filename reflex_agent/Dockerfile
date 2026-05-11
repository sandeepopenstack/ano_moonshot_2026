FROM python:3.12-slim

WORKDIR /app/reflex_agent
COPY . .
RUN pip install --no-cache-dir -r requirements.txt

WORKDIR /app
ENV AGENT_MODEL=gemini-2.5-flash
EXPOSE 8000

CMD ["adk", "api_server", "--port=8000", "/app"]