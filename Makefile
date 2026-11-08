.PHONY: install test lint fmt validate generate promtool evaluate run gate screenshot docker clean

PORT ?= 8835

install:
	uv sync --python 3.12

test:
	uv run pytest -q

lint:
	uvx ruff check .

fmt:
	uvx ruff check --fix .

validate:
	uv run sloforge validate examples/slos --policy examples/policy.yaml

# Every strategy, so promtool checks all of them.
generate:
	rm -rf out && uv run sloforge generate examples/slos --out out --all-strategies
	uv run sloforge check-rules out

# Needs Docker. CI runs the same command.
promtool: generate
	docker run --rm --entrypoint promtool -v $(PWD)/out:/out prom/prometheus check rules /out/*.yaml

# 90 simulated days, every strategy scored. Writes docs/results.json.
evaluate:
	uv run sloforge evaluate --out docs/results.json

run:
	uv run sloforge serve --port $(PORT)

# Ask a running server whether a feature deploy is allowed. Exit code 1 means blocked.
gate:
	uv run sloforge gate $(SERVICE) --change feature --url http://127.0.0.1:$(PORT)

screenshot:
	uv run python scripts/screenshot.py --port $(PORT)

docker:
	docker build -t sloforge:local .

clean:
	rm -rf out .pytest_cache .ruff_cache
