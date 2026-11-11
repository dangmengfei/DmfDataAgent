.PHONY: install dev test lint mysql migration

install:
	pip install -e ".[rag,dev]"

dev:
	uvicorn dmf_agent.api.app:app --reload --host 0.0.0.0 --port 8000

test:
	pytest -v

lint:
	ruff check dmf_agent tests

mysql:
	docker compose up -d mysql

# 首次生成迁移（需 MySQL 已启动、模型已建好）：
#   alembic revision --autogenerate -m "init"
migration:
	alembic upgrade head
