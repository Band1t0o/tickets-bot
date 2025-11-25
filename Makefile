.PHONY: install run watch fmt pw-install

install:
	python -m venv .venv && . .venv/bin/activate && pip install -U pip && pip install -r requirements.txt

run:
	. .venv/bin/activate && python -m src.cli scrape

watch:
	. .venv/bin/activate && python -m src.cli watch

fmt:
	. .venv/bin/activate && python - <<'PY' \
import subprocess, sys; \
subprocess.run([sys.executable, '-m', 'pip', 'install', 'ruff'], check=True); \
subprocess.run(['ruff', 'check', '--select', 'I', '--fix', 'src'], check=False)
PY

pw-install:
	. .venv/bin/activate && python -m playwright install --with-deps
