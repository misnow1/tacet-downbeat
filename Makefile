.DEFAULT_GOAL := help
VENV := .venv
PY := $(VENV)/bin/python

.PHONY: help venv install lint fmt typecheck test check hooks clean

help:  ## Show this help
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-10s %s\n", $$1, $$2}'

venv:  ## Create the virtualenv (pyenv supplies the interpreter)
	test -d $(VENV) || python3 -m venv $(VENV)

install: venv  ## Install the project and its dev tooling
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev]"

lint:  ## Ruff lint
	$(VENV)/bin/ruff check .

fmt:  ## Ruff format in place, fixing what it can
	$(VENV)/bin/ruff check --fix .
	$(VENV)/bin/ruff format .

typecheck:  ## mypy, strict
	$(VENV)/bin/mypy

test:  ## Run the suite
	$(VENV)/bin/pytest -q

check: lint typecheck test  ## Everything CI runs
	$(VENV)/bin/ruff format --check .

hooks: install  ## Install the pre-commit hooks
	$(VENV)/bin/pre-commit install

clean:  ## Remove build and cache artefacts
	rm -rf .mypy_cache .pytest_cache .ruff_cache build dist *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
