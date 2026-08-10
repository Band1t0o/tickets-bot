# Serves the control panel. Searching itself belongs to the scheduled GitHub
# Actions sweep, which commits results back to the repo - this image is for
# reading them somewhere other than the machine that ran `make ui`.
#
# It used to end in `CMD python -m src.cli scrape`, a command that no longer
# exists, and installed all three Playwright engines when the providers only
# ever drive chromium.
FROM python:3.12-slim
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && python -m playwright install --with-deps chromium

COPY . .
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "src.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
