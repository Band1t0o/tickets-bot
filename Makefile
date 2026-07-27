.PHONY: install run watch fmt pw-install

install:
	python -m venv .venv && . .venv/bin/activate && pip install -U pip && pip install -r requirements.txt

run:
	. .venv/bin/activate && python -m src.cli scrape

watch:
	. .venv/bin/activate && python -m src.cli watch

fmt:
	. .venv/bin/activate && pip install -q ruff && ruff check --select I --fix src

pw-install:
	. .venv/bin/activate && python -m playwright install --with-deps
