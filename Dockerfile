# Chat Room: website (HTTP + WebSocket) and raw TCP chat server.
# Uses only the Python standard library, so there is nothing to pip install.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY chat ./chat

# 8000 = website/WebSocket (or $PORT on hosting platforms), 5000 = terminal/GUI clients
EXPOSE 8000 5000
CMD ["python", "-m", "chat.server"]
