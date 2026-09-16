# Payment Operations Service

Сервис проводит платёжные операции через внешнего провайдера (`provider-simulator`)
и гарантирует идемпотентность и корректное состояние при повторах, конкурентных
запросах, потерянных ответах и перезапусках.

**Главный инвариант:** при любых повторах, конкурентных запросах и перезапусках
одной операции соответствует не более одного платежа у провайдера, а финальный
статус (`COMPLETED` / `REJECTED`) ставится **только** по callback-квитанции.

---

## Запуск

Требуется Docker и Docker Compose v2.

```bash
docker compose up --build
```

Поднимаются три сервиса:

- `postgres` — постоянное хранилище (named volume `candidate-data`)
- `candidate-service` — сервис кандидата на порту `8080`
- `provider-simulator` — симулятор провайдера на порту `8081`

Дождись в логах `candidate-service` строк:

```
db.schema.applied
app.start
worker.start
Application startup complete.
```

Остановить: `docker compose down`.
Удалить вместе с данными: `docker compose down -v`.

---

## Сквозной сценарий

В отдельном терминале:

```bash
OP=op-demo-$(date +%s)
BASE=http://localhost:8080

# 1. Создать операцию
curl -s -X POST $BASE/operations \
  -H 'Content-Type: application/json' \
  -d "{\"operationId\":\"$OP\",\"amount\":\"1000.00\",\"currency\":\"RUB\",\"description\":\"demo\"}"
echo

# 2. Запустить отправку
curl -s -o /dev/null -w "submit: HTTP %{http_code}\n" \
  -X POST $BASE/operations/$OP/submit

# 3. Подождать, пока провайдер пришлёт квитанцию
sleep 5

# 4. Текущее состояние
curl -s $BASE/operations/$OP | python -m json.tool

# 5. История переходов
curl -s $BASE/operations/$OP/events | python -m json.tool
```

Ожидаемо:

- `submit: HTTP 202`
- `status: COMPLETED` (или `REJECTED` в зависимости от режима симулятора),
  `providerPaymentId` — UUID, выданный провайдером;
- `events`: ровно 3 записи — `CREATED → SUBMITTED → COMPLETED`.

## Автоматическая проверка

В репозитории есть два независимых сценария сквозной проверки. Выбери тот,
что подходит под окружение.

### Вариант A: Python доступен (рекомендуется)

Требуется только Python 3.10+ (стандартная библиотека) и запущенный Docker Compose.
Никаких `pip install`.

```bash
docker compose up --build -d
docker compose ps           # все три сервиса должны быть Up
python3 verify.py
```

`verify.py` использует `urllib.request` из стандартной библиотеки, поэтому
не требует установки зависимостей.

### Вариант B: Python недоступен

Требуется только `bash` и `curl` (есть на любой Unix-подобной системе).

```bash
docker compose up --build -d
docker compose ps
bash verify.sh
```

`verify.sh` парсит JSON через `grep`/`sed`, покрывает те же сценарии,
что и `verify.py`.

### Что покрывают оба сценария

| Раздел | Что проверяется |
|---|---|
| 0 | `/health` отвечает `200`; все три сервиса `Up` |
| 1 | Создание → `CREATED`; дубль → `409`; submit ×2 (`202` + `200`); финал `COMPLETED`; 3 события с правильными типами |
| 2 | Аудит провайдера: один `payment accepted`, один `callback delivered`, `replay:false` |
| 3 | 10 параллельных `submit` → ровно один `202`, девять `200`, три события |
| 4 | Дубликат → `204`; поздняя противоположная → `204`; чужой `ppid` → `409`; неизвестная операция → `404`; финал не меняется |
| 5 | `REJECTED` через ручную квитанцию; `submit` после финала → `200`; 2 события |
| 6 | Рестарт `candidate-service` во время `PROCESSING`; ровно один платёж в аудите |
| 7 | `docker compose down && up` (без `-v`) — данные и история сохраняются |
| 8 | Валидация: отрицательная сумма, не-`RUB`, пропущенный `operationId`, три знака после точки → `422`; неизвестная операция → `404` |

### Ожидаемый вывод (оба варианта)

```
════════════════════════════════════════════════════════════════
 ИТОГ
════════════════════════════════════════════════════════════════
  PASS: 42
  FAIL: 0

  🎉 ВСЁ ЗЕЛЁНОЕ
```

Exit-код: `0` — всё зелёное, `1` — есть падения.

### Если упало

Скрипт выведет список провалов с `expected` и `actual`. Частые причины:

- **`health` не отвечает** — стек не поднялся, смотри `docker compose ps`.
- **`payment accepted` ≠ 1** — в аудите провайдера остались старые записи. Помогает:
  ```bash
  docker compose down -v
  docker compose up --build -d
  ```
- **`down && up` уронил данные** — проверь, что используется `docker compose down`
  без флага `-v`, и что volume жив:
  ```bash
  docker volume ls | grep candidate
  ```
---

## API

| Метод | Маршрут | Успех | Назначение |
|---|---|---|---|
| `GET` | `/health` | `200` | Проверка готовности |
| `POST` | `/operations` | `201` | Создать операцию. Дубль → `409` |
| `POST` | `/operations/{id}/submit` | `202` / `200` | Запланировать отправку |
| `POST` | `/receipts` | `204` | Принять callback-квитанцию |
| `GET` | `/operations/{id}` | `200` | Текущее состояние |
| `GET` | `/operations/{id}/events` | `200` | История переходов |

### POST /operations

```json
{
  "operationId": "operation-123",
  "amount": "1000.00",
  "currency": "RUB",
  "description": "Оплата заказа"
}
```

`operationId` обязателен. `amount` — положительная десятичная строка
с не более чем двумя знаками после точки. `currency` — только `RUB`.

Ответ:

```json
{
  "operationId": "operation-123",
  "amount": "1000.00",
  "currency": "RUB",
  "description": "Оплата заказа",
  "status": "CREATED",
  "providerPaymentId": null
}
```

Повторное создание с тем же `operationId` → `409 Conflict`.

### POST /operations/{id}/submit

- Первый вызов: `202 Accepted`. Операция переходит `CREATED → PROCESSING`,
  намерение отправки сохраняется в БД **до** внешнего HTTP-вызова.
- Повторный вызов при `PROCESSING`, `COMPLETED`, `REJECTED`: `200 OK`
  с текущим состоянием. Новое намерение не создаётся.
- При 10 параллельных `submit` ровно один вернёт `202`, остальные — `200`.

### POST /receipts

```json
{
  "providerPaymentId": "aa5b7856-e9f2-4fd5-955b-38b1f28d9c57",
  "operationId": "operation-123",
  "result": "COMPLETED",
  "message": "Payment completed",
  "occurredAt": "2026-07-15T12:00:00Z"
}
```

Правила:

- первая валидная квитанция ставит финальный статус → `204`;
- повтор той же квитанции → `204`, без нового перехода;
- поздняя квитанция с противоположным результатом → `204`,
  помечается как проигнорированная, финальный статус не меняется;
- чужой `providerPaymentId` (не совпадает с уже установленным) → `409`;
- неизвестная операция → `404`.

### GET /operations/{id}/events

```json
[
  {
    "eventId": 1,
    "type": "CREATED",
    "fromStatus": null,
    "toStatus": "CREATED",
    "message": "Operation created",
    "occurredAt": "2026-07-15T12:00:00Z"
  }
]
```

`eventId` монотонно возрастает в пределах операции.

---

## Архитектура

```
                ┌─────────────┐
  HTTP ───────► │  FastAPI    │  routes.py
                └──────┬──────┘
                       ▼
                ┌─────────────┐
                │ repository  │  вся работа с БД, транзакции
                └──────┬──────┘
                       ▼
                ┌─────────────┐
                │ PostgreSQL  │  operations, events, receipts_seen
                └──────┬──────┘
                       ▲
                       │
                ┌──────┴──────┐         ┌────────────────────┐
                │   worker    │ ──────► │ provider-simulator │
                │  (asyncio)  │ ◄────── │      (httpx)       │
                └─────────────┘         └────────────────────┘
                       ▲
                       │ callback POST /receipts
                       │
```

Ключевые компоненты:

- **`src/repository.py`** — единственное место, где меняется состояние.
  Все мутации внутри `transaction()` с `SELECT ... FOR UPDATE` там, где нужно.
- **`src/worker.py`** — фоновый поллинг БД. Не ставит финальный статус.
  Живёт в одном asyncio-таске внутри того же процесса, что и HTTP-сервер.
- **`src/provider.py`** — HTTP-клиент к провайдеру с retry, backoff и jitter.
- **`src/routes.py`** — тонкий слой: валидация → репозиторий → сериализация.

### Схема БД

| Таблица | Назначение |
|---|---|
| `operations` | Одна строка = одна операция. Источник истины по статусу и `provider_payment_id` |
| `events` | Журнал переходов. `PRIMARY KEY (operation_id, event_id)` |
| `receipts_seen` | Защита от повторных квитанций. `PRIMARY KEY (operation_id, provider_payment_id, result)` |

---

## Как обеспечиваются инварианты

### Одна операция = один платёж провайдера

- При `submit` строка операции блокируется `SELECT ... FOR UPDATE`.
  Только один параллельный вызов переводит `CREATED → PROCESSING`.
  Остальные видят уже `PROCESSING` и возвращают `200`.
- Все вызовы `POST /payments` идут с `Idempotency-Key = operationId`.
  Провайдер гарантирует, что повтор с тем же ключом вернёт тот же
  `providerPaymentId` и не создаст второй платёж.

### Намерение отправки сохраняется до вызова

- В `submit` транзакция коммитится со статусом `PROCESSING` **до** вызова провайдера.
- Если процесс падает между коммитом и HTTP-вызовом — после рестарта
  воркер находит `PROCESSING` с `next_attempt_at` и продолжает отправку
  с тем же `Idempotency-Key`.

### Финальный статус — только из квитанции

- Ни `POST /payments`, ни retry, ни работа воркера не ставят `COMPLETED` / `REJECTED`.
- Финальный переход делает `apply_receipt` в одной транзакции с записью
  в `receipts_seen` и `events`.

### Callback может прийти раньше HTTP-ответа

- `POST /receipts` не требует предварительного `providerPaymentId`.
  Если он ещё не сохранён — устанавливается из квитанции.
- Поздний `202` от провайдера после этого не сбрасывает статус:
  `save_provider_payment_id` не трогает финальные операции.

---

## Ограничения

Callback-квитанция от провайдера доставляется **один раз**. Симулятор делает
несколько быстрых попыток (≈3 с интервалом ~0.5 с), но не переотправляет
квитанцию после успешной доставки. Если все попытки попали в окно рестарта
`candidate-service` — квитанция безвозвратно потеряна.

В этом случае:

- worker продолжает периодически (раз в 30 секунд) вызывать `POST /payments`
  с тем же `Idempotency-Key`;
- провайдер отвечает `replay:true` — второго платежа **не создаётся**;
- операция остаётся в статусе `PROCESSING`.

Это **не баг**, а прямое следствие контракта: финальный статус ставится
только по квитанции, и никакой ретрай нашего сервиса не может заменить
недоставленный callback. Главный инвариант — «одна операция = один платёж
у провайдера» — сохраняется в любом сценарии, что подтверждается аудитом
провайдера (`replay:true` для повторных вызовов).

---

## Тесты

```bash
pip install -e ".[dev]"
pytest
```

Тесты требуют запущенного Docker — Postgres поднимается в отдельном
контейнере через `docker run` и останавливается после прогона.

Покрытие:

- `tests/test_api.py` — HTTP-контракт, валидация, коды ответов;
- `tests/test_provider.py` — retry, backoff, `Idempotency-Key` (через `respx`);
- `tests/test_repository.py` — конкурентный `submit`, обработка квитанций,
  изоляция транзакций.

Ожидаемый результат: **19 passed** за ~10 секунд.

---

## Переменные окружения

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `DATABASE_URL` | `postgresql://app:app@localhost:5432/app` | DSN PostgreSQL |
| `PROVIDER_URL` | `http://localhost:8081` | Базовый URL провайдера |
| `LOG_LEVEL` | `INFO` | Уровень логирования (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `PORT` | `8080` | Порт HTTP-сервера |

В Docker Compose значения задаются в `compose.yaml`.

---

## Стек

- Python 3.14
- FastAPI + uvicorn
- Pydantic v2
- httpx (async)
- PostgreSQL 16
- asyncpg (сырой SQL, без ORM)
- Docker + Docker Compose

---

## Структура проекта

```
.
├── compose.yaml
├── Dockerfile
├── .dockerignore
├── .gitignore
├── pyproject.toml
├── README.md
├── src/
│   ├── __init__.py
│   ├── config.py       # настройки из env
│   ├── db.py           # пул asyncpg + transaction()
│   ├── models.py       # Pydantic-схемы
│   ├── provider.py     # httpx-клиент к провайдеру
│   ├── repository.py   # все SQL-запросы
│   ├── routes.py       # HTTP-роуты
│   ├── main.py         # FastAPI + lifespan + JSON-логи
│   ├── worker.py       # фоновый поллинг PROCESSING
│   └── schema.sql      # DDL
└── tests/
    ├── conftest.py
    ├── test_api.py
    ├── test_provider.py
    └── test_repository.py
```

---

## Как проверялось

Ручной прогон сценариев из списка автопроверки:

1. Базовый путь до `COMPLETED` через реального `provider-simulator`. ✅
2. 10 параллельных `submit` — ровно один `202`, остальные `200`. ✅
3. Дубликат квитанции — `204`, без нового перехода. ✅
4. Поздняя противоположная квитанция — `204`, финал не меняется. ✅
5. Чужой `providerPaymentId` — `409`. ✅
6. Рестарт `candidate-service` во время обработки — операция доходит до
   финала (либо остаётся `PROCESSING` при потере callback, см. «Ограничения»). ✅
7. Сохранность данных при `docker compose down && up` (без `-v`). ✅
8. Аудит провайдера: `replay:false` — ровно один платёж на операцию. ✅