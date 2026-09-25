.PHONY: up down reset ingest test lint eval seed

up:        ## build and start Postgres + API (loads the committed snapshot on first boot)
	docker compose up --build -d --wait && echo "API: http://localhost:8000/docs"

down:
	docker compose down

reset:     ## drop the database volume and reload the snapshot
	docker compose down -v && $(MAKE) up

ingest:    ## refresh everything from SEC EDGAR + Yahoo Finance
	docker compose run --rm ingest

test:      ## unit + API tests inside the api container
	docker compose run --rm --entrypoint pytest api

lint:
	docker compose run --rm --entrypoint sh api -c "ruff check . && ruff format --check . && mypy app"

eval:      ## run the /ask evaluation set (needs LLM_API_KEY) and rewrite docs/eval_results.md
	docker compose run --rm -v "$(PWD)/docs:/app/docs" --entrypoint python api scripts/eval_questions.py --write

seed:      ## export the current database to db/init/02_seed.sql.gz
	docker compose run --rm -v "$(PWD)/db/init:/app/db/init" --entrypoint python api scripts/export_seed.py
