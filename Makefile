# ChainTwin -- thin wrapper over run.py, for machines that have make.
# On Windows without make, call run.py directly:  python run.py all
PY ?= python

.PHONY: help up down all schema seed optimise anchor benchmark verify demo tamper test app clean

help:
	@$(PY) run.py

up schema seed optimise anchor benchmark verify demo tamper test app all down clean:
	$(PY) run.py $@
