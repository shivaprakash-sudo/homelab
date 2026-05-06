FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Volumes expected:
#   /downloads  → downloaded video files
#   /session    → persisted Telethon session file

CMD ["python", "-u", "main.py"]
