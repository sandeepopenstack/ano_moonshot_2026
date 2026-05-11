FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV AGENT_MODEL=gemini-2.5-flash

EXPOSE 8000

CMD ["adk", "web", "reflection_agent/"]
