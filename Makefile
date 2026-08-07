.PHONY: install run watch fmt pw-install test ui sweep dry-run probe probe-report

# Python 3.12+ is required: config/models use `str | None`, which pydantic
# evaluates at runtime and 3.9 cannot parse.
PY ?= python3.12

install:
	$(PY) -m venv .venv && . .venv/bin/activate && pip install -U pip && pip install -r requirements.txt

run:
	. .venv/bin/activate && python -m src.cli scrape

watch:
	. .venv/bin/activate && python -m src.cli watch

fmt:
	. .venv/bin/activate && pip install -q ruff && ruff check --select I --fix src

pw-install:
	. .venv/bin/activate && python -m playwright install --with-deps chromium

test:
	. .venv/bin/activate && pytest -q

ui:
	. .venv/bin/activate && uvicorn src.web.app:app --reload --port 8000
