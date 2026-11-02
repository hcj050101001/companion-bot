.PHONY: sync test lint cov

sync:
	uv sync --locked

# 与 CI 同口径：统一走 scripts/test.sh（pytest -q --timeout=10）。
test:
	./scripts/test.sh

lint:
	uv run ruff check .

cov:
	uv run pytest -q --timeout=10 --cov=companion --cov-report=term
