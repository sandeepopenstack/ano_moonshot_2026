FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV AGENT_MODEL=gemini-2.5-flash
ENV TOOLBOX_URL=http://localhost:5000

EXPOSE 8000

CMD ["adk", "web", "reflex_agent/"]
