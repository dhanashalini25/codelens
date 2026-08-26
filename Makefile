.PHONY: help install test demo serve docker clean

DEMO_REPO ?= /tmp/codelens-demo

help:
	@echo "make install   install dependencies"
	@echo "make demo      build a flawed demo repo and review it"
	@echo "make test      run the test suite"
	@echo "make serve     run the API on http://localhost:8000"
	@echo "make docker    build and run the container"
	@echo "make clean     delete the local index and demo repo"

install:
	pip install -r requirements.txt

test:
	python -m pytest tests -q

demo:
	rm -rf $(DEMO_REPO)
	python demo/make_demo_repo.py $(DEMO_REPO)
	python -m codelens.cli index $(DEMO_REPO)
	python -m codelens.cli review $(DEMO_REPO)

serve:
	uvicorn codelens.api:app --reload --port 8000

docker:
	docker build -t codelens .
	docker run --rm -p 8000:8000 -v codelens-data:/data codelens

clean:
	rm -rf .codelens $(DEMO_REPO)
