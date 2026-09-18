# Разработка

Python 3.12. Создайте venv, установите `pip install -r requirements-dev.lock` и
`pip install --no-deps -e .`.

Проверки перед коммитом:

```sh
ruff check .
ruff format --check .
pytest -q
python scripts/export_openapi.py
alembic upgrade head
alembic check
```

Для PostgreSQL задайте `ASR_DATABASE_URL` и `TEST_DATABASE_URL` на **отдельную тестовую базу**.
Тесты удаляют содержимое тестовых таблиц. CI прогоняет одну и ту же suite на SQLite и PostgreSQL.
ASR/LLM в unit-тестах заменяются тестовыми адаптерами; успешный CI не доказывает качество распознавания.

Ветки: `codex/<change>` или `feature/<change>`. Небольшие коммиты с описанием причины изменения,
pull request в main, CI перед merge. Ключи и аудиозаписи в Git не добавлять.

Изменение базы: обновите модель, затем `alembic revision --autogenerate -m "description"`.
Проверьте SQL и downgrade, добавьте тест миграции; перед обновлением живой базы сделайте backup.
Начальная миграция зафиксирована и не зависит от будущих изменений моделей.

Обновление lock-файлов: `pip-compile --extra test --output-file requirements-dev.lock pyproject.toml`
и `pip-compile --output-file requirements.lock pyproject.toml` под Python 3.12.
Lock-файлы фиксируют версии; контейнер и OS-пакеты обновляются отдельно.
