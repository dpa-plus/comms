.PHONY: test build install clean version

PYTHON ?= python3
VENV   := python/.venv
PY     := $(VENV)/bin/python

$(PY):
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install -e "python[dev]"

test: $(PY)
	cd python && .venv/bin/python -m pytest -q

build: $(PY)
	cd python && .venv/bin/python -m build --outdir dist .

# Installs from this checkout the way users do, then runs `doctor --fix`.
install:
	sh install.sh --from python

version:
	@sed -n 's/^__version__ = "\(.*\)"/\1/p' python/comms_graph/__init__.py

clean:
	rm -rf python/dist python/build python/*.egg-info
