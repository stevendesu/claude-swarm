FROM python:3.12-slim
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY server.py .
COPY static/ ./static/
ENV TICKET_DB=/tickets/tickets.db
EXPOSE 3000
CMD ["python3", "server.py"]
