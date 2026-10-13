# Backend планирования питания

Небольшой сервер для будущего приложения питания под бюджет и целевые КБЖУ
для одного человека в России. `meal_budget` — техническое, не продуктовое имя.
Клиент планируется на Flutter: настройки, меню и заметки останутся в SQLite
на телефоне, без регистрации и пользовательских аккаунтов.

## Что работает сейчас

- FastAPI, Pydantic v2, Uvicorn и настройки через pydantic-settings.
- `GET /health` — проверка процесса без обращения к БД или DeepSeek.
- `GET /v1/products` — три демонстрационных продукта из JSON.
- `POST /v1/menu-calculations` — КБЖУ присланного меню, покупки целых упаковок и бюджет.
- Валидация каталога при запуске; повреждённый файл прерывает запуск.
- `/docs`, `/redoc`, `/openapi.json` — стандартная документация FastAPI.
- Отдельный сервис DeepSeek: структурированное меню, проверка ядром и до двух исправлений.
- Ручной CLI генерации из JSON-файла.
- Закрытый `POST /v1/menu-generations` для разработчика: выключен по умолчанию,
  отдельный Bearer-токен, одна генерация на процесс и тело до 64 KiB.

Данные каталога помечены `demo_unverified`: цены и КБЖУ не проверены,
использовать их для реального планирования питания нельзя.
Серверной БД, аккаунтов и фоновых заданий пока нет. DeepSeek вызывается только
при явном CLI-запуске или авторизованном POST после включения endpoint.
По умолчанию API не требует ключа или модели DeepSeek.

## Установка и запуск

Нужны Python 3.13 и uv (для воспроизведения окружения используется uv 0.12.5).
Если uv отсутствует, стандартная установка без изменения системного Python:
`curl -LsSf https://astral.sh/uv/0.12.5/install.sh | sh`.
Все следующие команды выполняются из корня репозитория:

```bash
uv sync --frozen
uv run --frozen uvicorn meal_budget.main:app --app-dir src --reload
```

Зависимости зафиксированы в `uv.lock`, окружение создаётся в `.venv`.
Не используйте параллельно pip/requirements.txt. uv может установить отдельный
Python 3.13 в пользовательский каталог; системный Python не меняется.
Без reload: уберите `--reload`. Локальный адрес: `http://127.0.0.1:8000`.

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/v1/products
curl -X POST http://127.0.0.1:8000/v1/menu-calculations \
  -H 'Content-Type: application/json' --data-binary @examples/menu-calculation.json
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pytest
```

Пример расчета — техническая проверка API, не рекомендация по питанию.
Все продукты покупаются с нуля, без учета запасов и готовых остатков.

## Структура

```text
src/meal_budget/
  main.py          # create_app(), lifespan и приложение
  catalog.py       # чтение и проверка локального JSON
  api/             # тонкие HTTP-маршруты
  core/            # конфигурация
  schemas/         # каталог, расчет и контракт сервиса генерации
  services/        # расчетное ядро, DeepSeek transport и проверка генерации без FastAPI
  generate_menu.py # ручной CLI, не HTTP endpoint
data/products.json # демонстрационный каталог
tests/             # автономные API- и unit-тесты
docs/              # продукт, архитектура, API, решения, roadmap
```

Расчет отделен от генерации; пустые интерфейсы не создаются.
Из read-only референса `styl-backend` адаптированы фабрика приложения,
lifespan, разделение роутеров/схем/логики, настройки, uv и подход к проверкам.
Его бизнес-логика, инфраструктура и зависимости не перенесены.

## Переменные окружения

Необязательно: `cp .env.example .env`, затем заполнить локальные настройки.
Окружение имеет приоритет над `.env`, который читается из корня проекта.
Никогда не коммитьте `.env` и ключи.

| Переменная | По умолчанию / назначение |
| --- | --- |
| `APP_ENVIRONMENT` | `local`; также `test`, `staging`, `production` |
| `CATALOG_PATH` | `data/products.json`; относительный путь от корня проекта, не cwd |
| `DEEPSEEK_API_KEY` | Пусто; серверный секрет, для CLI только из окружения |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com`; к нему добавляется `/chat/completions`, допустим `/v1` |
| `DEEPSEEK_MODEL` | Пусто в Settings; пример `.env.example`: `deepseek-flash` |
| `DEEPSEEK_THINKING` | `disabled`; также `enabled` или `omit` для модели без этого параметра |
| `DEEPSEEK_REASONING_EFFORT` | `low`; только с enabled, также `high`/`max` |
| `DEEPSEEK_CONNECT_TIMEOUT` / `READ_TIMEOUT` / `WRITE_TIMEOUT` / `POOL_TIMEOUT` | С префиксом `DEEPSEEK_`: 10 / 60 / 10 / 10 секунд |
| `DEEPSEEK_MAX_OUTPUT_TOKENS` | 8192; предел настройки 32768 на вызов |
| `DEEPSEEK_MAX_RESPONSE_BYTES` | 262144; полный ответ, включая thinking и keep-alive; максимум 1048576 |
| `GENERATION_TOTAL_TIMEOUT` | 180 секунд на весь цикл; максимум 600 |
| `GENERATION_HTTP_ENABLED` | `false`; включает закрытый endpoint разработчика |
| `GENERATION_ACCESS_TOKEN` | Пусто; отдельный серверный SecretStr, обязателен при включении; печатный ASCII без пробелов |
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
Ключ введите без отображения и записи в shell history; CLI не читает `.env`.
В bash из корня проекта:

```bash
uv sync --frozen
read -r -s -p 'DeepSeek API key: ' DEEPSEEK_API_KEY
export DEEPSEEK_API_KEY
export DEEPSEEK_MODEL=deepseek-flash
export DEEPSEEK_THINKING=disabled
PYTHONPATH=src uv run --frozen python -m meal_budget.generate_menu \
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
игнорируется Git, а JSON каталога и примеров — нет. Не храните анкеты и ключи в Git.
Проверка правила: `git check-ignore generated/personal-menu.json generated/tests/run-1.json`.
Коды выхода: 0 — успех,
1 — ошибка генерации, 2 — ошибка локальных файлов/входных данных/конфигурации Settings.
Отсутствующие ключ/модель — ошибка генерации `configuration_missing` (код выхода 1).

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
Нелюбимые продукты — предупреждение, не запрет. Бюджет берется только из анкеты,
покупки с нуля и целыми упаковками. Каждый дневной показатель сравнивается с
неокругленным расчетом ядра; допуск — продуктовая настройка, не медицинская норма.
Свободный текст пожеланий — данные, не системные инструкции. **Промпт снижает риск
неправильных инструкций, но не заменяет серверную проверку.**
Проверяются структура, данные и заявленное время, но не реальное приготовление,
безопасность рецепта, медицинская полноценность или качество цен/КБЖУ.

Автономная проверка CLI и интеграции через подмененный HTTP-транспорт:

```bash
uv run --frozen pytest tests/test_generate_menu_platform.py tests/test_deepseek_headers.py tests/test_menu_generation.py
```

Тесты и CI не запускают платный сценарий. Публичный endpoint появится только
вместе с заданиями, токенами установок, идемпотентностью и защитой расходов.

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
В bash из корня проекта, терминал сервера (не используйте shell tracing):

```bash
set +x
uv sync --frozen
read -r -s -p 'DeepSeek API key: ' DEEPSEEK_API_KEY
export DEEPSEEK_API_KEY
read -r -s -p 'Generation access token (отдельный секрет): ' GENERATION_ACCESS_TOKEN
export GENERATION_ACCESS_TOKEN
export GENERATION_HTTP_ENABLED=true
export DEEPSEEK_MODEL=deepseek-flash
export DEEPSEEK_THINKING=disabled
uv run --frozen uvicorn meal_budget.main:app --app-dir src \
  --host 127.0.0.1 --port 8000 --workers 1
# После остановки сервера:
unset DEEPSEEK_API_KEY GENERATION_ACCESS_TOKEN GENERATION_HTTP_ENABLED
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
GET каталога неденежные Decimal-поля остаются строками. Подробности в [API](docs/API.md).
PostgreSQL сейчас не требуется. Если появится, деньги — `NUMERIC(..., 2)`,
миграции — yoyo-migrations, не Alembic. yoyo пока не установлен.

## Docker

```bash
docker build -t meal-budget:local .
docker run --rm -p 8000:8000 meal-budget:local
```

Контейнер работает под UID/GID 10001 без reload, устанавливает runtime-зависимости
из lock-файла, содержит демонстрационный каталог. `.env` и секреты не копируются;
настройки передаются через окружение при запуске. Compose не нужен.
`/app/generated` доступен UID 10001 для ручного CLI; результаты внутри контейнера
эфемерны. Для сохранения вне контейнера можно отдельно подключить каталог в
`/app/generated` с правами на запись UID 10001. Генерация при запуске API не выполняется.

## Документация

- [Правила разработки](AGENTS.md)
- [Текущий статус и последние проверки](docs/STATUS.md)
- [Продукт и MVP](docs/PRODUCT.md)
- [Архитектура](docs/ARCHITECTURE.md)
- [API](docs/API.md)
- [Решения](docs/DECISIONS.md)
- [Последовательные этапы](docs/ROADMAP.md)
