.PHONY: install run watch fmt lint pw-install test test-ui ui sweep dry-run probe probe-report

# 3.10+ is the real floor: models use `str | None`, which pydantic evaluates at
# runtime and 3.9 cannot parse. CI pins 3.12; the suite passes on 3.11.
PY ?= python3.12

# Windows has no `. .venv/bin/activate`, and calling the venv interpreter
# directly works on every platform.
VENV_BIN := $(if $(wildcard .venv/Scripts/python.exe),.venv/Scripts,.venv/bin)
PYTHON := $(VENV_BIN)/python

install:
	$(PY) -m venv .venv && $(PYTHON) -m pip install -U pip && $(PYTHON) -m pip install -r requirements-dev.txt

pw-install:
	$(PYTHON) -m playwright install --with-deps chromium

test:
	$(PYTHON) -m pytest -q -m "not slow"

# Drives the route editor in a real browser. Needs `make pw-install`.
test-ui:
	$(PYTHON) -m pytest -q -m slow

lint:
	$(PYTHON) -m ruff check .

fmt:
	$(PYTHON) -m ruff check --fix .

ui:
	$(PYTHON) -m uvicorn src.web.app:app --reload --port 8000

# These were declared .PHONY but never existed, so `make sweep` silently did
# nothing. They exist now.
sweep:
	$(PYTHON) -m src.cli sweep --scenario $(SCENARIO) $(if $(DEPTH),--depth $(DEPTH),)

dry-run:
	$(PYTHON) -m src.cli sweep --scenario $(SCENARIO) --dry-run

probe:
	$(PYTHON) -m src.cli probe

probe-report:
	$(PYTHON) -m src.cli probe-report
