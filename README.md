# Backend планирования питания

Небольшой сервер для будущего приложения питания под бюджет и целевые КБЖУ
для одного человека в России. `meal_budget` — техническое, не продуктовое имя.
Клиент планируется на Flutter: настройки, меню и заметки останутся в SQLite
на телефоне, без регистрации по телефону/email и пользовательских аккаунтов.

## Что работает сейчас

- FastAPI, Pydantic v2, Uvicorn и настройки через pydantic-settings.
- `GET /health` — проверка процесса без обращения к БД или DeepSeek.
- `GET /v1/products` — версионированный JSON-каталог с происхождением данных.
- `POST /v1/menu-calculations` — КБЖУ присланного меню, покупки целых упаковок и бюджет.
- Валидация каталога при запуске; повреждённый файл прерывает запуск.
- `/docs`, `/redoc`, `/openapi.json` — стандартная документация FastAPI.
- Отдельный сервис DeepSeek: структурированное меню, проверка ядром и до двух исправлений.
- Ручной CLI генерации из JSON-файла.
- Закрытый `POST /v1/menu-generations` для разработчика: выключен по умолчанию,
  отдельный Bearer-токен, одна генерация на процесс и тело до 64 KiB.
- MEAL-8: закрытые `POST /v1/generation-jobs` (202) и `GET /v1/generation-jobs/{id}`;
  один worker, ограниченная очередь и временное хранение в PostgreSQL.
- MEAL-9: установки с отдельным Bearer, принадлежащие им app-задания,
  PostgreSQL-идемпотентность и суточные квоты; отдельное включение, по умолчанию выключено.

MEAL-5: импортированы **70 SKU** отдельно собранного снимка 5ka.ru
(`5ka-2026-10-14-observed-v3`), включая кешированные представления карточек.
Регион/магазин, касса и условия покупки без карты **не подтверждены**:
`price_type=displayed`, `single_package_without_card=null`, `demo_unverified`.
Это не актуальная московская корзина. 79 кандидатов сохранены отдельно, не рассчитываются.
Правила и ограничения — [CATALOG](docs/CATALOG.md). ordinary (3×3) и weekly (7×3)
готовы по ручным witness, четыре других сценария blocked. Стоимость контрольных
корзин 4718,80 / 6800,69 ₽, бюджет 4950 / 7150 ₽; ежедневные цели
2075 ккал / 125 Б / 67 Ж / 245 У, допуск ±10%. ordinary 2026-10-15 завершился
`attempts_exhausted`; после MEAL-6 запуск 2026-10-17 с thinking enabled / high
дал проверенное меню за три вызова. Первые два ответа — `incomplete_response`,
по 8192 completion tokens, но сохраненного `finish_reason` нет: упор в лимит не доказан.
Новые платные запуски требуют отдельного согласования.
MEAL-6 сокращает контекст каталога и добавляет безопасную историю проверок в локальный
`--report`. MEAL-7 сохраняет там причины завершения ответов. Пользователь выполнил
ordinary с лимитом 16384: **success за один вызов, 30,21 с, finish_reason=stop**,
15 078 токенов; бюджет и КБЖУ прошли проверки. Это один результат, не доказательство
влияния увеличенного лимита или стабильности. weekly пока не запускался — отдельно
после оценки ordinary. Итог — [STATUS](docs/STATUS.md), команды и формат —
[EVALUATION](docs/EVALUATION.md).
По умолчанию `data/products.json` содержит прежние три **синтетических demo-продукта**
(`demo-meal-3-v1`), не реальные товары «Пятёрочки». Обычный запуск и прежние
технические примеры работают без выбора fixture. Тесты используют отдельные fixtures.
Аккаунтов нет. PostgreSQL хранит установки, счётчики квот и временные задания, не профиль и
не мобильные данные. DeepSeek вызывается только при явном CLI-запуске,
авторизованном синхронном POST или выполнении принятого задания.
По умолчанию API не требует ключа или модели DeepSeek.

## Установка и запуск

Нужны Python 3.13 и uv (для воспроизведения окружения используется uv 0.12.5).
Если uv отсутствует, стандартная установка без изменения системного Python:
`curl -LsSf https://astral.sh/uv/0.12.5/install.sh | sh`.
Все следующие команды выполняются из корня репозитория:

```bash
uv sync --frozen
uv run --frozen python manage.py
```

Зависимости зафиксированы в `uv.lock`, окружение создаётся в `.venv`.
Не используйте параллельно pip/requirements.txt. uv может установить отдельный
Python 3.13 в пользовательский каталог; системный Python не меняется.
Локальный адрес: `http://127.0.0.1:8000`. По умолчанию один worker, без reload.
Для разработки без генерационного HTTP endpoint: `uv run --frozen python manage.py --reload`.
При `GENERATION_HTTP_ENABLED=true` launcher запрещает reload; настройки секретов
не принимает через аргументы и не выводит. Порт/адрес меняются через `--port`/`--host`.
`--help` доступен без загрузки настроек и запуска приложения.
`manage.py` определяет пути от своего файла, поэтому работает также при запуске
абсолютным путем из другой рабочей директории, без ручного PYTHONPATH.

Если удобно активировать окружение в терминале:

```bash
source .venv/bin/activate
python manage.py
```

### PyCharm

Для запуска HTTP-сервера выбирайте корневой **`manage.py`**.
Interpreter: `.venv/bin/python` (Python 3.13), Script path: `$PROJECT_DIR$/manage.py`,
Working directory: `$PROJECT_DIR$`; без дополнительных параметров.
Для этого рабочего проекта подготовлена локальная конфигурация **Meal Budget API**:
выберите ее в списке Run/Debug. Если IDE еще не подхватила файлы, переоткройте проект.
`.idea/` не входит в Git; на другой машине конфигурацию нужно создать вручную.
Корень проекта отмечается Sources Root, `tests/` — Test Sources Root, `.venv/`
исключается из исходников. Системный Python 3.12 и глобальные настройки IDE менять не требуется.

Корневой `main.py` собирает приложение в `create_app()`: подключает маршруты,
обработчики ошибок и middleware, владеет каталогом, HTTPX-клиентом, пулом PostgreSQL и
worker через lifespan (генерационные ресурсы — только при включении HTTP-генерации).
`manage.py` отвечает только за запуск Uvicorn. Альтернативный запуск из корня:

```bash
uv run --frozen uvicorn main:create_app --factory \
  --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/v1/products
curl -X POST http://127.0.0.1:8000/v1/menu-calculations \
  -H 'Content-Type: application/json' --data-binary @examples/menu-calculation.json
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pytest -m "not postgres"
uv run --frozen python tools/test_postgres.py -q
```

Пример расчета — техническая проверка API, не рекомендация по питанию.
По умолчанию пример возвращает 200 с расчетом 89.90 руб. по demo-данным.
Для явного выбора реального снимка:
`CATALOG_PATH=data/5ka-products.json uv run --frozen python manage.py`.
В этом снимке новые ID `5ka_*`: прежний пример с demo-ID дает 422 `unknown_product_id`.
Для генерации ordinary использовать синтетическую анкету из examples/evaluation/,
не прежний demo-пример. **Автоматического fallback на demo нет**;
поврежденный/отсутствующий выбранный файл также не подменяется другим каталогом.
Все продукты покупаются с нуля, без учета запасов и готовых остатков.

## Структура

```text
main.py            # create_app(), lifespan, ресурсы, маршруты и middleware
manage.py          # запуск API через Uvicorn, в том числе из PyCharm
meal_budget/
  catalog.py       # чтение и проверка локального JSON
  api/             # тонкие HTTP-маршруты
  core/            # конфигурация
  schemas/         # каталог, расчет и контракт сервиса генерации
  services/        # расчетное ядро, DeepSeek transport и проверка генерации без FastAPI
  generate_menu.py # ручной CLI, не HTTP endpoint
  migrate_jobs.py  # отдельные apply/rollback PostgreSQL через yoyo
migrations/        # версионированная серверная схема, не миграции Flutter
compose.yaml       # PostgreSQL с volume/healthcheck; API — профиль api
data/products.json # синтетический demo-каталог по умолчанию
data/5ka-products.json # 70 наблюдаемых SKU, явный CATALOG_PATH, demo_unverified
data/5ka-candidates.json # 79 кандидатов вне расчетов, не ProductCatalog
examples/evaluation/ # синтетические анкеты, ручные witness и manifest
tools/             # автономный импорт и перепроверка подготовки без DeepSeek
tests/             # автономные API- и unit-тесты
docs/              # продукт, архитектура, API, решения, roadmap
```

Расчет отделен от генерации; пустые интерфейсы не создаются.
Пакет `meal_budget/` находится рядом с точками входа: нет адаптеров сборки,
вложенного main.py или ручного изменения sys.path. Импорты пакета работают
при запуске корневого manage.py и модулей из корня проекта.

## Переменные окружения

Необязательно: `cp .env.example .env`, затем заполнить локальные настройки.
Окружение имеет приоритет над `.env`, который читается из корня проекта.
Никогда не коммитьте `.env` и ключи.

| Переменная | По умолчанию / назначение |
| --- | --- |
| `APP_ENVIRONMENT` | `local`; также `test`, `staging`, `production` |
| `CATALOG_PATH` | `data/products.json` — demo; `data/5ka-products.json` — реальный снимок; путь от корня проекта, не cwd |
| `DEEPSEEK_API_KEY` | Пусто; серверный секрет, для CLI только из окружения |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com`; к нему добавляется `/chat/completions`, допустим `/v1` |
| `DEEPSEEK_MODEL` | Пусто в Settings; пример `.env.example`: `deepseek-flash` |
| `DEEPSEEK_THINKING` | `disabled`; также `enabled` или `omit` для модели без этого параметра |
| `DEEPSEEK_REASONING_EFFORT` | `low`; только с enabled, также `high`/`max` |
| `DEEPSEEK_CONNECT_TIMEOUT` / `READ_TIMEOUT` / `WRITE_TIMEOUT` / `POOL_TIMEOUT` | С префиксом `DEEPSEEK_`: 10 / 60 / 10 / 10 секунд |
| `DEEPSEEK_MAX_OUTPUT_TOKENS` | 8192; предел настройки 32768 на вызов; передается как `max_tokens`; 16384 — только явный эксперимент |
| `DEEPSEEK_MAX_RESPONSE_BYTES` | 262144; полный ответ, включая thinking и keep-alive; максимум 1048576 |
| `GENERATION_TOTAL_TIMEOUT` | 180 секунд на весь цикл; максимум 600 |
| `GENERATION_HTTP_ENABLED` | `false`; включает закрытый endpoint разработчика |
| `GENERATION_APP_ENABLED` | `false`; включает установки и app-задания; требует также `GENERATION_HTTP_ENABLED=true` |
| `GENERATION_APP_DAILY_INSTALLATION_LIMIT` | `3`; новых принятых app-задания на установку за UTC-сутки (1–1000) |
| `GENERATION_APP_DAILY_GLOBAL_LIMIT` | `30`; новых принятых app-заданий всех установок за UTC-сутки (1–10000) |
| `INSTALLATIONS_DAILY_LIMIT` | `20`; новых установок за UTC-сутки (1–10000) |
| `INSTALLATIONS_TOTAL_LIMIT` | `200`; всего установок в таблице (1–100000), без автоматического удаления |
| `GENERATION_ACCESS_TOKEN` | Пусто; отдельный серверный SecretStr, обязателен при включении; печатный ASCII без пробелов |
| `GENERATION_JOBS_DATABASE_URL` | Пусто, SecretStr; PostgreSQL URL из защищенного окружения; нужен только для HTTP-генерации/миграций |
| `GENERATION_JOBS_POOL_SIZE` | `4`; максимум соединений psycopg async pool, от 1 до 10; минимум пула — 1 |
| `GENERATION_JOBS_DB_TIMEOUT` | `5` секунд; ожидание пула/старта, connect timeout (округляется вверх), SQL statement/lock timeout; максимум 60 |
| `GENERATION_JOBS_QUEUE_LIMIT` | `4`; от 1 до 100 ожидающих, отдельно от одного активного задания |
| `GENERATION_JOBS_RETENTION_SECONDS` | `86400` (24 часа) после completed/failed; максимум 7 суток |
| `GENERATION_JOBS_CLEANUP_INTERVAL` | `60` секунд между очистками простаивающего worker; максимум 3600 |
| `GENERATION_NUTRITION_TOLERANCE` | `0.10`: каждый показатель каждого дня в ±10%, границы включительно |

Отсутствие ключа не мешает запуску. Модель не выбирается по модели OpenCode.
Файл каталога обязательно должен быть доступен при startup, включая health.
После старта health не обращается к каталогу или внешним зависимостям.

## Ручная генерация MEAL-2

**Файловый сценарий CLI поддерживается и проверен только на Linux**, с
`os.O_DIRECTORY` и `os.O_NOFOLLOW`. На другой платформе или без этих возможностей
CLI завершается с понятным сообщением и кодом 2 до файловых операций, без traceback.
Импорт модуля и `--help` доступны; переноса файловой защиты на Windows нет,
небезопасного fallback без `O_NOFOLLOW` нет.

Официальная документация проверена 2026-10-11: экономичная `deepseek-flash`
(DeepSeek-V4.1-Flash) поддерживает JSON Output и оба режима thinking.
Имя модели — конфигурация приложения, не модель OpenCode. Провайдер по умолчанию
включает thinking, поэтому мы явно отправляем `disabled`; при `enabled` — effort `low`.
Источники: [Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion),
[модели](https://api-docs.deepseek.com/quick_start/pricing),
[JSON Output](https://api-docs.deepseek.com/guides/json_mode),
[thinking](https://api-docs.deepseek.com/guides/thinking_mode),
[ошибки](https://api-docs.deepseek.com/quick_start/error_codes),
[keep-alive и лимиты](https://api-docs.deepseek.com/quick_start/rate_limit).
JSON Output может вернуть пустой ответ; `finish_reason=length` означает обрезание.
Нестриминговый API может присылать пустые строки keep-alive и закрывает соединение
через 10 минут ожидания начала inference; наши read/общий timeout существенно короче.

**Следующая команда платная и в рамках реализации не запускалась с DeepSeek.**
Пример ниже технический, по синтетическому demo-каталогу; не сценарий оценки MEAL-4.
Для выбранного реального снимка готовые анкеты ordinary/weekly и команда оценки —
в [EVALUATION](docs/EVALUATION.md); технический demo-пример ниже не сценарий MEAL-5.
Пустой каталог по-прежнему дает `catalog_unavailable` до провайдера.
Ключ введите без отображения и записи в shell history; CLI не читает `.env`.
В bash из корня проекта:

```bash
uv sync --frozen
read -r -s -p 'DeepSeek API key: ' DEEPSEEK_API_KEY
export DEEPSEEK_API_KEY
export DEEPSEEK_MODEL=deepseek-flash
export DEEPSEEK_THINKING=disabled
uv run --frozen python -m meal_budget.generate_menu \
  --request examples/generation-request.json --output personal-menu.json
unset DEEPSEEK_API_KEY
```

Пример анкеты — технический тест на один прием пищи, **не дневная рекомендация**.
Результат включает план, независимый расчет, `demo_unverified`, предупреждения,
использованный допуск и метаданные каждого ответа: фактическая модель и token usage,
если получены, плюс число вызовов и общую длительность. Тариф/стоимость не выдумываются.
Результат сохраняется в `generated/personal-menu.json` от корня проекта, не cwd.
`--output` задает **относительный путь внутри `generated/`**, например
`personal-menu.json` или `tests/run-1.json`; каталог и вложенные директории
создаются автоматически. Абсолютные пути, `..` и символические ссылки
(включая сам generated/, родительские каталоги и файл) запрещены.
Файл публикуется атомарно с правами 0600, не заменяется без `--overwrite`.
Временные файлы создаются в том же защищенном каталоге; `/generated/` целиком
игнорируется Git, а JSON каталога и примеров — нет. Не храните реальные персональные
анкеты и ключи в Git; curated синтетические анкеты examples/evaluation/ отслеживаются.
Проверка правила: `git check-ignore generated/personal-menu.json generated/tests/run-1.json`.
Коды выхода: 0 — успех,
1 — ошибка генерации, 2 — ошибка локальных файлов/входных данных/конфигурации Settings.
Отсутствующие ключ/модель — ошибка генерации `configuration_missing` (код выхода 1).
Если GenerationError сочетается со сбоем записи `--report`, код выхода — **2**:
приоритет остается у ошибки локального файла. В stderr сначала выводятся исходный
безопасный код/описание генерации, затем отдельное сообщение о невозможности
сохранить сводку. Текст OSError/traceback не выводится, генерация не повторяется.

Если явно задан `--scenario`, CLI до провайдера проверяет существование ID,
`status=ready`, подготовленную анкету, версию выбранного каталога и доказательство:
положительный witness пересчитывается ядром, нижняя граница невозможного случая
сверяется по доступным товарам. Анкета --request должна совпадать с подготовленной.
Blocked/неизвестный/неподготовленный сценарий дает понятную ошибку и код 2, даже
с непустым каталогом. Без --scenario обычная генерация не зависит от manifest;
в необязательном --report она обозначается как `scenario=unspecified`.

Output/report проверяются совместно до создания родительских директорий и до
провайдера: лексически нормализованные пути не могут совпадать или быть вложены
один в другой; существующие препятствующие файлы/директории и symlink запрещены.
Внешние гонки не устранены: атомарная публикация защищает каждый файл отдельно,
меню и report не являются атомарной парой.

Одно предложение + максимум два исправления (не более 3 вызовов); скрытые сетевые
повторы и переходы по redirect отключены. 401/403, 429, 5xx и сетевой timeout
завершают цикл сразу. Невалидный JSON, схема, пустой/обрезанный/слишком большой
ответ и нарушения ограничений используют тот же бюджет исправлений.
Authorization получает нормализованный ключ без внешних ASCII-пробелов;
пустые ключи, управляющие и непечатные/non-ASCII символы отвергаются до HTTPX,
включая управляющие символы на краях. Исходный ключ не включается в ошибки.
Логирование httpcore не отключается.
Отправляется `Accept-Encoding: identity`; любой другой Content-Encoding
отвергается до чтения тела. Byte-limit применяется к `aiter_raw()`, без распаковки.
Предыдущий план отправляется только как разобранный JSON-объект до 64 KiB;
невалидный текст и reasoning_content не отправляются повторно.
После каждого исправления повторяется полная проверка. При исчерпании попыток:
«Не удалось подобрать меню с указанными условиями», без утверждения о невозможности.
При timeout расход токенов может быть неизвестен; ручной повтор может снова стоить денег.

Каталог с состояниями, КБЖУ, упаковками и ценами передается отдельно от анкеты,
исключенные ID заранее удаляются. Неизвестные ID предпочтений отвергаются до вызова.
Представление `PromptProduct` содержит только id, name, state, state_basis,
nutrition_per_100g, package_size_g и price. Все разрешенные товары доступны модели,
без скрытого отбора. Происхождение, URL, timestamps и deprecated-поля остаются в
исходном каталоге/API; общие ограничения наблюдаемых цен, географии, условий и
as_sold передаются один раз в `catalog_context`. JSON компактен, схема ответа сохранена.
Нелюбимые продукты — предупреждение, не запрет. Бюджет берется только из анкеты,
покупки с нуля и целыми упаковками. Каждый дневной показатель сравнивается с
неокругленным расчетом ядра; допуск — продуктовая настройка, не медицинская норма.
Свободный текст пожеланий — данные, не системные инструкции. **Промпт снижает риск
неправильных инструкций, но не заменяет серверную проверку.**
Проверяются структура, данные и заявленное время, но не реальное приготовление,
безопасность рецепта, медицинская полноценность или качество цен/КБЖУ.

При `--report` локально сохраняются номер и стадия проверки каждой завершенной
попытки, безопасные коды/пути и actual/границы бюджета и КБЖУ. До 20 нарушений и
12 KiB на попытку, с полным числом нарушений и признаком усечения. Сохраняется и
история до успеха после исправления, и причины исчерпания попыток. Сырые ответы,
неудачные меню, анкеты/wishes, ключи, заголовки и сообщения ValidationError не пишутся.
Это не расширяет HTTP-ответы. Старый отчет восстановить до такой истории нельзя.
С MEAL-7 отдельное локальное поле `response_metadata` содержит для каждого
полученного ответа номер `attempt` и безопасный `finish_reason`: `stop`, `length`,
`content_filter`, `tool_calls`, `insufficient_system_resource`, `other` или null.
Сохраняются и ответы, отвергнутые через `InvalidResponse`. Только `stop` допускает
дальнейшую проверку меню; `length` отвергается даже при валидном JSON.
Неизвестный текст заменяется на `other`, отсутствующая/недоступная причина — null;
у старых отчетов все поле `response_metadata=null`. `length` сообщает об ограничении
длины, но не доказывает, какой именно лимит сработал. При timeout, превышении
byte-limit или поврежденном JSON причина может быть недоступна. HTTP metadata,
OpenAPI и файл проверенного результата не расширены. Подробности — EVALUATION.
Обычный сбой сборщика диагностики не меняет результат генерации: в рамках этого
запуска сборщик отключается, сохраняется `available=false`, а violation_count и
truncated равны null. Это неизвестная диагностика, **не отсутствие нарушений**.
Резервная запись не использует сломанный сборщик и не включает текст исключения.
Если невозможна и она, история может быть неполной; считать ее доказательством
отсутствия нарушений нельзя. Отмена, KeyboardInterrupt и SystemExit не подавляются.

Автономная проверка CLI и интеграции через подмененный HTTP-транспорт:

```bash
uv run --frozen pytest tests/test_generate_menu_platform.py tests/test_deepseek_headers.py tests/test_menu_generation.py
```

Тесты и CI не запускают платный сценарий. API MEAL-9 рассчитан на закрытый пилот;
для публичного запуска требуются дополнительные меры abuse-защиты (см. ниже).

## Закрытая HTTP-генерация MEAL-3

Это **доступ разработчика для тестов, не публичный API для Flutter**.
Не встраивайте `GENERATION_ACCESS_TOKEN` в будущий клиент. Endpoint выключен
по умолчанию (404 и отсутствует в OpenAPI), не создает HTTPX-клиент и не требует
DeepSeek-ключа. Включение без непустого корректного access token прерывает сборку
приложения понятной ошибкой конфигурации без значения секрета.
Ключ/модель провайдера проверяются сервисом: отсутствие дает 503, не платный вызов.

Только **один процесс Uvicorn, `--workers 1`, без reload**. Один слот на процесс;
конкурентный авторизованный POST немедленно получает 429, без очереди и вызова
DeepSeek. Bearer проверяется через `secrets.compare_digest` до чтения тела;
лимит **65536 байт**, включая chunked, действует до полного JSON-разбора (413).
HTTPX-клиент переиспользуется через lifespan и закрывается при shutdown;
отмена задачи закрывает поток ответа и освобождает слот, общий клиент остается
доступен следующим запросам. Старые health/каталог/расчет не вызывают провайдера.

ASGI `root_path` поддерживается, в том числе вложенный: guard и обработчики ошибок
сопоставляют путь тем же способом, что Starlette/FastAPI. Endpoint требует серверное
подтверждение guard для конкретного запроса; если middleware не отработал, генерация
отвергается с 401 даже с правильным Bearer. Завершающий `/` не обходит защиту:
только после проверок возможен 307 на канонический маршрут, без вызова провайдера.
При `--root-path /api` внешний proxy должен удалить `/api` из передаваемого пути:
внешний `/api/v1/menu-generations` → upstream `/v1/menu-generations`.
Сам параметр Uvicorn не удаляет префикс из входного HTTP URL. Поэтому при прямой
проверке Uvicorn curl отправляется на `/v1/menu-generations`; Location редиректа
содержит внешний `/api`. Не добавляйте `-L` к платному POST автоматически.

Сервис прежний: одно предложение + до двух исправлений — **до трех платных
вызовов на один POST**, общий deadline 180 секунд по умолчанию, без автоматических
сетевых повторов. Возвращается только проверенный `GenerationResult` с расчетом,
`demo_unverified`, предупреждением каталога и доступными моделью/usage.
Нет идемпотентности: повторный POST — новая потенциально платная генерация.
Таймаут или отключение клиента **не гарантируют отсутствие расходов**;
Uvicorn не обязан отменять обработчик при разрыве соединения. Слот в этом случае
может оставаться занятым до завершения/общего deadline. Меню на сервере не сохраняется.

**Следующий пример потенциально платный; во время реализации с DeepSeek не запускался.**
Ниже технический demo-пример MEAL-3, не оценка импортированного снимка MEAL-5.
Для будущей оценки использовать подготовленную анкету и явный каталог из
[EVALUATION](docs/EVALUATION.md). Без DeepSeek-ключа включенный endpoint возвращает
503 `configuration_missing`, calls=0; пустой каталог — `catalog_unavailable`.
В bash из корня проекта, терминал сервера (не используйте shell tracing):

Предварительно запустите PostgreSQL по разделу «Фоновые задания MEAL-8»;
для включенной HTTP-генерации он теперь обязателен.

```bash
set +x
uv sync --frozen
read -r -s -p 'DeepSeek API key: ' DEEPSEEK_API_KEY
export DEEPSEEK_API_KEY
read -r -s -p 'Generation access token (отдельный секрет): ' GENERATION_ACCESS_TOKEN
export GENERATION_ACCESS_TOKEN
read -r -s -p 'PostgreSQL URL: ' GENERATION_JOBS_DATABASE_URL
export GENERATION_JOBS_DATABASE_URL
export GENERATION_HTTP_ENABLED=true
export DEEPSEEK_MODEL=deepseek-flash
export DEEPSEEK_THINKING=disabled
uv run --frozen python -m meal_budget.migrate_jobs apply
uv run --frozen python manage.py
# После остановки сервера:
unset DEEPSEEK_API_KEY GENERATION_ACCESS_TOKEN GENERATION_HTTP_ENABLED GENERATION_JOBS_DATABASE_URL
```

В другом терминале введите **тот же access token**, но не DeepSeek-ключ.
`curl --disable` отключает пользовательский curlrc (в том числе скрытые retries),
`--noproxy '*'` исключает proxy для локального запроса. Токен передается через
stdin-заголовок (`--header @-`), не argv. Не используйте `-v`, `--trace`,
`--retry` или автоматическое повторение POST. Результат — новый файл внутри
игнорируемого `generated/`, с правами 0600 благодаря mktemp/umask:

```bash
set +x
umask 077
mkdir -p generated
read -r -s -p 'Generation access token: ' GENERATION_ACCESS_TOKEN
output=$(mktemp generated/http-menu.XXXXXX.json)
printf 'Authorization: Bearer %s\n' "$GENERATION_ACCESS_TOKEN" | \
  curl --disable --noproxy '*' --silent --show-error --fail --header @- \
  --header 'Content-Type: application/json' \
  --data-binary @examples/generation-request.json \
  --output "$output" http://127.0.0.1:8000/v1/menu-generations
unset GENERATION_ACCESS_TOKEN
```

При HTTP-ошибке curl завершается с ненулевым кодом; файл может остаться пустым,
это не меню. Анкета примера — техническая проверка, не дневная рекомендация.
HTTP-коды и формат ошибок — в [API](docs/API.md).
Автономная HTTP-проверка, включая живой Uvicorn и локальный тестовый провайдер:
`uv run --frozen pytest tests/test_generation_http.py tests/test_generation_root_path.py tests/test_generation_http_live.py`.

## Деньги и хранение

Расчёты и внутренние денежные значения — `Decimal` в рублях, максимум два
значимых десятичных разряда после запятой. JSON возвращает денежные значения
числами, например `89.9` (равно `89.90`), а не строками и не копейками.
Преобразование в float допускается только в JSON-сериализаторе, не в расчётах.
Загрузка JSON-каталога использует `parse_float=Decimal`.
В расчете все числовые показатели возвращаются JSON-числами; в существующем
GET каталога, plan, КБЖУ, grams, servings и nutrition_tolerance тоже возвращаются
JSON-числами. Подробности и переход контракта — в [API](docs/API.md).
Сервер заданий — PostgreSQL, миграции — yoyo-migrations, не Alembic.
Анкета/результат — JSONB: Decimal сохраняется точными десятичными строками без
промежуточного float; наружу по-прежнему отдаются JSON-числа. Отдельных денежных
столбцов сейчас нет; при появлении использовать NUMERIC(scale 2), не FLOAT.
Каталог пока остается JSON, телефон в будущем использует отдельную SQLite.

## Фоновые задания MEAL-8

**Закрытый API разработчика, не авторизация публичного Flutter.** Оба новых
маршрута выключены вместе с HTTP-генерацией и защищены тем же серверным Bearer.
Токены установок, квоты и идемпотентность реализованы в отдельном API MEAL-9 ниже;
developer-маршруты намеренно сохраняют прежнее поведение.
**Повторный POST создает новое потенциально платное задание**, в том числе после
потери ответа 202. Не повторяйте POST автоматически.

При выключенной HTTP-генерации прежние маршруты и ручной CLI работают без БД.
Перед включением `GENERATION_HTTP_ENABLED=true` поднимите PostgreSQL и примените
миграции отдельной командой. Compose не читает реальный `.env` в примерах ниже:

```bash
set +x
read -r -s -p 'Пароль локального PostgreSQL: ' POSTGRES_PASSWORD
export POSTGRES_PASSWORD
docker compose --env-file /dev/null up -d --wait db
# URL с URL-encoded credentials: host 127.0.0.1, port 5432, database meal_budget.
read -r -s -p 'PostgreSQL URL: ' GENERATION_JOBS_DATABASE_URL
export GENERATION_JOBS_DATABASE_URL
uv sync --frozen
uv run --frozen python -m meal_budget.migrate_jobs apply
# После настройки Bearer и провайдера, см. локальный безопасный пример ниже:
uv run --frozen python manage.py
```

Rollback — **только для тестовой/выведенной из работы БД**, удаляет таблицу и
ВСЕ задания. После него для следующего запуска снова нужен apply:

```bash
uv run --frozen python -m meal_budget.migrate_jobs rollback
uv run --frozen python -m meal_budget.migrate_jobs apply
```

Подключение задается `GENERATION_JOBS_DATABASE_URL` (`postgresql://` или `postgres://`).
Не передавайте DSN/пароль в argv, не выводите настройки, не включайте shell tracing
или `docker compose config` без `--quiet`: вывод может содержать секреты.
Миграции не запускаются в lifespan; без соединения/схемы включенный API прерывает
startup с безопасным сообщением. При выключенной генерации пул и клиент провайдера
не открываются. Один драйвер **psycopg 3**: async API + ограниченный psycopg_pool,
синхронный backend yoyo `postgresql+psycopg` в отдельном процессе. Пул закрывается
после worker/HTTPX. До 16 ожидающих получения соединения, затем/при DB timeout —
безопасный 503. Восстановление соединений пула не повторяет внешнюю генерацию.
Миграции и API не должны работать одновременно. Rollback первой версии удаляет
таблицу и данные безвозвратно; роль миграций требует DDL, приложению достаточно DML.

Рабочая история — **только `migrations/postgresql/`**, независимая первая версия
`001_generation_jobs_postgresql`. SQLite-миграция находится в `migrations/archive_sqlite/`;
доступного оригинала нет, ее происхождение/побайтовая неизменность не подтверждены.
Архив не реконструируется; команда его не читает, образ его не содержит.
Новые миграции должны явно зависеть от предыдущих; примененные не переписываются.

### Переход с локального SQLite-прототипа

Остановите старый API, сохраните SQLite-файлы приватно; новая версия их не читает,
не удаляет и **не переносит автоматически**. Создайте отдельную пустую PostgreSQL БД,
выполните apply, замените старый `GENERATION_JOBS_DB_PATH` на новый URL и запустите
один API. Старые задания/результаты не появятся в новой БД: GET старого ID дает 404.
Для требуемого переноса нужна отдельная согласованная процедура. Не повторяйте
старые running и не пересоздавайте POST автоматически: токены могли быть потрачены.
Старые файлы/sidecar и `var/` остаются в `.gitignore` как защита данных.

Только **один процесс/worker Uvicorn, без reload и реплик**. Один worker генерации
использует общий активный слот с прежним синхронным endpoint. Пока он занят,
синхронный POST получает 429; задания ожидают в PostgreSQL. При заполнении очереди
новый POST получает 429 `generation_queue_full` без обращения к провайдеру.
Предел считается по `queued`, не по уже выполняющемуся заданию; уменьшение лимита
после перезапуска не удаляет ранее принятые задания. FIFO — по времени создания и ID.
Общий deadline начинается при выполнении, не при ожидании в очереди; прежние
три вызова максимум, отсутствие сетевых retries и общий HTTPX-клиент сохранены.

Задание сохраняется до отправки 202 и больше не принадлежит HTTP-запросу:
disconnect клиента не отменяет работу. GET дает queued/running либо completed
с проверенным `GenerationResult`, либо failed с безопасным `error_code`, без
сырого ответа/анкеты. Некорректная схема анкеты отвергается до создания (422),
доменные ошибки генерации фиксируются как failed. Версия каталога фиксируется
при приеме. После перезапуска queued выполняются только по той же версии;
иначе failed `catalog_version_mismatch`. Старые completed можно получить и после
смены каталога: результат содержит свою исходную версию.
Running после сбоя помечаются failed `interrupted`, **никогда не повторяются**:
токены уже могли быть потрачены. Штатный shutdown отменяет активный цикл, закрывает
поток, фиксирует interrupted, оставляет queued следующему запуску и закрывает
HTTPX-клиент и пул. Это не гарантирует отсутствие расходов у провайдера.

Принятие — успешный COMMIT; admission и переход worker в недоступность сериализованы.
POST, оставшийся на cleanup во время аварии worker, получает 503 `worker_unavailable`
без новой записи. Начатый admission завершается перед этим переходом; принятый COMMIT
не отменяется. После аварийной остановки polling queued/running также дает 503
`worker_unavailable`, а не здоровое ожидание; completed/failed и неизвестный ID
сохраняют 200/404 при доступной БД. Автоматического перезапуска worker нет;
нужен перезапуск API со штатным recovery. Подробные гарантии — в API/ARCHITECTURE.

Count + INSERT сериализованы transaction-scoped advisory lock в READ COMMITTED.
Захват queued → running — один UPDATE с FOR UPDATE SKIP LOCKED / RETURNING;
состояние и результат сохраняются одной транзакцией, ошибки приводят к rollback.
Транзакции/соединения не удерживаются во время DeepSeek. Общий активный слот,
один worker и startup-recovery пока **зависят от одного процесса**, это не поддержка
нескольких реплик. Exactly-once внешнего платного вызова не гарантируется:
после успешного запроса возможен сбой до сохранения результата. Автоповтора нет.

TTL отсчитывается от завершения completed/failed. Удаляются строка задания,
анкета и результат; queued/running не удаляются. Worker очищает при старте,
между заданиями и по интервалу простоя; POST/GET также выполняют очистку, поэтому
GET просроченного задания дает **404 `job_not_found`**, как и неизвестный ID.
Во время активной генерации фоновая очистка может отложиться до ее завершения;
GET все равно проверяет TTL. DELETE — логическое удаление в PostgreSQL;
MVCC/VACUUM, WAL и резервные копии не дают обещания немедленного физического стирания.
Данные не логируются; доступ, TLS, шифрование носителя и backup retention —
ответственность администратора БД.
Compose отключает запись SQL/параметров и подробных DETAIL ошибочной строки;
для внешнего PostgreSQL администратор должен обеспечить эквивалентную политику логов.

### Локальный пример без DeepSeek и платных запросов

Провайдер возвращает только фиксированный синтетический план под demo-каталог
и `examples/generation-request.json`. Для других анкет/каталогов результат может
не пройти проверки — это не настоящий генератор. Во всех командах ниже ключ
фиктивный, URL только localhost; реальные секреты вводить не нужно.

Терминал 1:

```bash
uv run --frozen python tools/local_generation_provider.py
```

Терминал 2 (явные настройки переопределяют локальный `.env`):

```bash
export CATALOG_PATH=data/products.json
export GENERATION_HTTP_ENABLED=true
export GENERATION_ACCESS_TOKEN=local-test-only
export POSTGRES_PASSWORD=local-demo-only
export POSTGRES_PORT=55432
export GENERATION_JOBS_DATABASE_URL=postgresql://meal_budget:local-demo-only@127.0.0.1:55432/meal_budget
docker compose --env-file /dev/null -p meal-budget-demo up -d --wait db
export DEEPSEEK_API_KEY=local-fake-key
export DEEPSEEK_MODEL=local-test
export DEEPSEEK_BASE_URL=http://127.0.0.1:9001
export DEEPSEEK_THINKING=disabled
uv run --frozen python -m meal_budget.migrate_jobs apply
uv run --frozen python manage.py
```

Терминал 3, создание и чтение (в Bash, без curl retries и автоматических редиректов):

```bash
set +x
umask 077
mkdir -p generated
accepted=$(mktemp generated/local-job.XXXXXX.json)
printf 'Authorization: Bearer local-test-only\n' | \
  curl --disable --noproxy '*' --silent --show-error --fail --header @- \
  --header 'Content-Type: application/json' --data-binary @examples/generation-request.json \
  --output "$accepted" http://127.0.0.1:8000/v1/generation-jobs
job_id=$(uv run --frozen python -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$accepted")
printf 'Authorization: Bearer local-test-only\n' | \
  curl --disable --noproxy '*' --silent --show-error --fail --header @- \
  "http://127.0.0.1:8000/v1/generation-jobs/$job_id"
```

Повторяйте только GET до completed/failed, не POST. Результат — технический
тест, не рекомендация. После остановки закройте тестовый shell или удалите
экспортированные настройки; не используйте тестовый access token на доступном извне API.

## API приложения MEAL-9: закрытый пилот

Отдельный токен для каждой установки, без телефона/email, IP и fingerprint.
Общий developer-токен **не передается Flutter**. Flutter пока не реализован:
в будущем сохраняет Bearer в защищённом хранилище ОС, профиль/меню/заметки — локально.
Сервер возвращает токен только один раз при `POST /v1/installations` (201,
`Cache-Control: no-store`); хранит только SHA-256 от криптографически случайного
секрета (32 random bytes, 256 бит, непрозрачный `mbi_…`), UUID установки и время.
Токен не истекает автоматически; восстановления, ротации и refresh сейчас нет.
Не помещайте его в URL, логи, crash-report, analytics или незащищённые backups.
Требуются HTTPS и редактирование Authorization на proxy/APM. При компрометации —
остановить доступ/обратиться к оператору; публичного API отзыва пока нет.
При потере токена/переустановке без сохранённого OS-secret создаётся новая установка:
доступ к прежним заданиям и ключам не восстанавливается. Если ОС сохранила секрет,
можно продолжать использовать прежнюю установку. Сам UUID доступа не даёт.

| `GENERATION_HTTP_ENABLED` | `GENERATION_APP_ENABLED` | Режим |
| --- | --- | --- |
| false | false | Старые health/каталог/расчёт и CLI, без PostgreSQL/worker |
| true | false | Только прежний developer API, новые маршруты 404/нет в OpenAPI |
| true | true | Developer API + установки + app API, одна БД/очередь/worker/слот |
| false | true | Ошибка конфигурации до startup; скрытого включения генерации нет |

Перед запуском остановите API, примените схему **отдельно**, затем включите оба флага.
Настройки `.env.example` каталога/thinking/model/max_tokens не менялись.

```bash
# GENERATION_JOBS_DATABASE_URL уже задан через защищённое окружение, не argv.
uv run --frozen python -m meal_budget.migrate_jobs apply
export GENERATION_HTTP_ENABLED=true
export GENERATION_APP_ENABLED=true
# Настройте отдельный developer Bearer и провайдера по примеру MEAL-8 выше.
uv run --frozen python manage.py
```

### Последовательность без платного провайдера

Используйте **только локальный тестовый провайдер и demo-каталог** из примера MEAL-8
выше, перезапустив API с `GENERATION_APP_ENABLED=true`. Следующий пример хранит
токен только в памяти, не печатает его и не передаёт через argv. При каждом запуске
примера создаётся новая установка; это демонстрация, не реализация клиентского storage.

```bash
uv run --frozen python - <<'PY'
import json
import time
import httpx

with httpx.Client(base_url="http://127.0.0.1:8000", trust_env=False, timeout=10) as client:
    issued = client.post("/v1/installations")
    issued.raise_for_status()
    token = issued.json()["token"]
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "local-demo-1"}
    with open("examples/generation-request.json") as source:
        questionnaire = json.load(source)
    accepted = client.post("/v1/app/generation-jobs", headers=headers, json=questionnaire)
    accepted.raise_for_status()
    job_id = accepted.json()["id"]
    print("Принято:", job_id)
    for _ in range(100):
        response = client.get(f"/v1/app/generation-jobs/{job_id}", headers=headers)
        response.raise_for_status()
        result = response.json()
        if result["state"] in ("completed", "failed"):
            print("Состояние:", result["state"])
            break
        time.sleep(0.2)
PY
```

В настоящем клиенте сохраняйте секрет, ключ и параметры **до POST**. При потере
ответа повторяйте тот же POST с тем же ключом и нормализованной анкетой, а не
регистрируйте установку/новый ключ автоматически. Повтор — 202 с прежним ID и
`state=queued` как подтверждением исходного приёма; актуальное состояние — GET.
Изменённая анкета с тем же ключом — 409. Ключ: 1–64 ASCII `[A-Za-z0-9_-]`,
один заголовок `Idempotency-Key`, чувствителен к регистру. JSON-порядок, пробелы
форматирования, одинаковые Decimal-значения, defaults и порядок/дубли списков ID
не создают различий. Значимые изменения `wishes` и других полей — создают.
Хранится SHA-256 валидированных нормализованных параметров, не вторая сырая анкета.
Версия каталога не часть хеша; повтор после её смены находит исходное задание.
Известное подтверждение можно получить и при остановленном worker; GET активного
задания по-прежнему даёт 503 `worker_unavailable`. Чужой/неизвестный ID — одинаковый 404.
Developer Bearer не подходит app API, installation Bearer — developer/sync API.
Закрытый developer API сохраняет административный доступ к временным заданиям.

Ключ живёт **вместе с заданием**: всё время queued/running, затем TTL от terminal
(24 ч по умолчанию, `GENERATION_JOBS_RETENTION_SECONDS`). После TTL GET → 404,
тот же ключ может создать **новое** задание с новым расходом квоты; вечной
идемпотентности нет. После истечения срока не делайте автоматический POST «для
восстановления результата». Потеря COMMIT-ответа может быть неопределённой, но в
пределах TTL повтор с тем же ключом разрешает её без повторного приёма.

Квоты считают **новые принятые задания**, не деньги: до трёх вызовов провайдера
на одно задание. Defaults 3/установку и 30/все установки за UTC-сутки — небольшой
закрытый пилот, не гарантированный денежный бюджет. Идемпотентные повторы,
невалидные запросы и отказы до COMMIT не расходуют квоту; accepted расходует её
даже после failed/interrupted, удаления по TTL или перезапуска. Счётчики отдельно
от заданий, сутки по часам PostgreSQL (UTC), reset при смене UTC-даты, старые
счётчики очищаются при следующем принятии. Общая квота не обходится регистрацией.
Developer/sync/CLI не входят в app-квоту и доступны только доверенному оператору.

429 включает безопасный код и `Retry-After`: квоты — до следующей UTC-полуночи,
полная очередь — 1 секунда (не гарантия освобождения). Регистрация ограничена
20/UTC-сутки и **200 всего** для защиты роста таблицы. Установки не удаляются
автоматически; при total-cap 429 `installation_capacity_reached`, Retry-After=86400 —
минимальная пауза, не обещание снятия ограничения, нужно решение оператора.
Потеря ответа регистрации может оставить недоступную клиенту строку, учитываемую в cap.

**Не готовность к открытому публичному запуску:** отдельный токен не доказывает
уникальность человека. Регистрационные cap можно исчерпать и вызвать отказ пилота;
нет attestation, защиты от ботов, per-source rate limits и полного abuse-контроля.
Нужны ограниченный доступ пилота на инфраструктуре, мониторинг, HTTPS, rate/body/
connection/time limits на proxy и отдельная политика отзыва/очистки установок.
Не добавляйте общий «секрет регистрации» в Flutter. App-квота не ограничивает
расходы доверенного developer API. **Exactly-once внешнего платного вызова нет**:
сбой после ответа провайдера до сохранения результата возможен, running не повторяется.

### Откат схемы MEAL-9

Только остановленный API и тестовая/выведенная БД, credentials через окружение.
`rollback --steps 1` на текущей схеме откатывает 002: удаляет app-задания,
установки/секреты, ключи и счётчики, **сохраняет прежние developer-задания**.
Токены после повторного apply не восстанавливаются. Не используйте rollback/reapply
как сброс квоты рабочего пилота. `rollback` без `--steps` как прежде откатывает
всю историю и удаляет также developer-задания.

```bash
uv run --frozen python -m meal_budget.migrate_jobs rollback --steps 1
uv run --frozen python -m meal_budget.migrate_jobs apply
```

## Docker

```bash
docker build -t meal-budget:local .
docker run --rm -p 8000:8000 meal-budget:local
```

Контейнер работает под UID/GID 10001 без reload, устанавливает runtime-зависимости
из lock-файла, содержит demo по умолчанию, отдельный снимок 70 SKU,
manifest с witness и две curated синтетические анкеты (не персональные данные).
`.env` и секреты не копируются;
настройки передаются через окружение при запуске. Для заданий Compose содержит
PostgreSQL 17 с healthcheck и volume; API — необязательный профиль `api`.
В образ копируются корневые main.py/manage.py и пакет meal_budget/;
сервер запускается `python manage.py --host 0.0.0.0 --port 8000` с одним worker.
`/app/generated` доступен UID 10001 для ручного CLI; результаты внутри контейнера
эфемерны. Для сохранения вне контейнера можно отдельно подключить каталог в
`/app/generated` с правами на запись UID 10001. Новые задания при старте не создаются;
при включении HTTP-генерации ранее принятые queued восстанавливаются по правилам MEAL-8.

Данные заданий теперь сохраняются **в volume PostgreSQL `postgres-data`**, не
в `/app/var` API. `docker compose down` сохраняет volume, `down --volumes` удаляет
БД — не применяйте последнее к рабочим данным. Для API в Compose URL должен
указывать на **db:5432**, а не localhost. Credentials вводите через окружение,
как выше; затем явные миграции перед стартом API:

```bash
docker compose --env-file /dev/null up -d --wait db
docker compose --env-file /dev/null --profile api build api
docker compose --env-file /dev/null --profile api run --rm --no-deps api \
  python -m meal_budget.migrate_jobs apply
docker compose --env-file /dev/null --profile api up -d --wait api
```

Без `GENERATION_HTTP_ENABLED=true` генерация по-прежнему выключена. Для localhost-провайдера
из контейнера нужен отдельный доступный сетевой адрес; `127.0.0.1` контейнера
не означает host. Не запускайте второй API/worker на той же БД.

## Проверки: автономные отдельно от PostgreSQL

```bash
uv sync --frozen
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pytest -m "not postgres"
# Сам создает уникальный Compose-проект, временные БД и удаляет только свои данные:
uv run --frozen python tools/test_postgres.py -q
```

Production smoke после `docker build -t meal-budget:local .`:
`uv run --frozen python tools/smoke_app_container.py`. Создаёт отдельные временные
Docker network/БД/API/синтетического провайдера, проверяет MEAL-9 и реальный SQL timeout
worker, recovery и сохранённые квоты; удаляет исключительно свои ресурсы.
Не читает `.env`, не использует credentials приложения и не вызывает DeepSeek.
Проверяет готовый runtime-образ без установки pytest внутрь него.

Автономные HTTP/worker-тесты используют адаптер только из tests/, не SQLite.
Интеграционные SQL-проверки выполняются на настоящем PostgreSQL. Альтернатива
для отдельного тестового сервера: `MEAL_TEST_POSTGRES_URL` с loopback host,
БД **meal_budget_test** и ролью CREATEDB, затем
`uv run --frozen pytest -m postgres tests/integration`. Каждому тесту создается
уникальная БД `meal_test_*`, после теста удаляется только она. URL приложения
и `.env` не используются. Без тестового PostgreSQL команда **падает**, не skip.
Обычный `pytest` включает интеграцию и потому также требует тестовый сервер.
GitHub Actions разделяет автономный job и PostgreSQL service/integration job.

Полный pytest/Ruff запускается **в dev-окружении** после `uv sync --frozen`, с
доступными git/curl для прежних интеграционных тестов. Это не проверка production-
образа: туда не устанавливаются pytest, git или curl ради тестов. Smoke-проверка
production-контейнера проверяет только включенные runtime-возможности и маршруты.
Если Docker не может получить base image/зависимости из-за сети, это отдельное
ограничение сборки, а не успех контейнерных тестов или причина скрывать тесты skip.

## Документация

- [Правила разработки](AGENTS.md)
- [Текущий статус и последние проверки](docs/STATUS.md)
- [Продукт и MVP](docs/PRODUCT.md)
- [Архитектура](docs/ARCHITECTURE.md)
- [API](docs/API.md)
- [Решения](docs/DECISIONS.md)
- [Последовательные этапы](docs/ROADMAP.md)
- [Происхождение каталога](docs/CATALOG.md)
- [Сценарии и ручная оценка](docs/EVALUATION.md)
