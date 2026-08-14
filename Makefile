.PHONY: audit check release reproducible test verify-release verify-results

PYTHON ?= python3
SOURCE_DATE_EPOCH ?= 1786579200

audit:
	$(PYTHON) scripts/release.py audit

verify-results:
	$(PYTHON) scripts/release.py verify-results

check: audit verify-results
	PYTHONPATH=src $(PYTHON) -m compileall -q src tests scripts
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v
	./spark-four-hca validate --config examples/pair.toml
	./spark-four-hca theory

test: check

reproducible: audit verify-results
	SOURCE_DATE_EPOCH=$(SOURCE_DATE_EPOCH) $(PYTHON) scripts/release.py reproducibility

release: audit verify-results
	SOURCE_DATE_EPOCH=$(SOURCE_DATE_EPOCH) $(PYTHON) scripts/release.py prepare --output-dir dist

verify-release:
	SOURCE_DATE_EPOCH=$(SOURCE_DATE_EPOCH) $(PYTHON) scripts/release.py verify-release --output-dir dist
