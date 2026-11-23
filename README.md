# Задача 28: Мессенджер учебных групп (упрощённый)

**Студент:** Яцко В.А.

## Постановка

API сообщений + PostgreSQL + Redis (публикация «последних» сообщений или online-статус). Compose: **три** сервиса (`api`, `db`, `redis`). Отправить сообщение в группу, последние N, список групп пользователя. Полный сценарий проверки в README.

## Что внутри

| Сервис | Образ | Роль | Порт наружу |
|---|---|---|---|
| `api` | собирается из `Dockerfile` (Python 3.12 + FastAPI) | REST API мессенджера | `${API_PORT:-8000}` → 8000 |
| `db` | `postgres:16-alpine` | источник истины: группы, участники, сообщения; том `study-messenger-pgdata` | **нет** (только сеть Compose) |
| `redis` | `redis:7-alpine` | кэш последних N сообщений группы + online-статус с TTL; том `study-messenger-redisdata` | **нет** |

```mermaid
flowchart LR
    U[curl / клиент] -- :8000 --> API[api · FastAPI]
    subgraph net[сеть study-messenger-net]
      API -- SQL --> DB[(db · PostgreSQL)]
      API -- кэш, online --> R[(redis)]
    end
```

* Сеть: именованная `study-messenger-net`. БД и Redis снаружи недоступны: порты не публикуются.
* Порядок старта: `api` ждёт `db` и `redis` по `depends_on: condition: service_healthy`
  (healthcheck: `pg_isready` и `redis-cli ping`), затем сам проходит healthcheck через `/health`.
* Схема БД создаётся при первом старте из `db/init.sql` (`/docker-entrypoint-initdb.d`).
* Секретов в репозитории нет: пароль БД берётся из `.env`, в репозитории только `.env.example`.

## Структура репозитория

```
app/main.py              # приложение (FastAPI)
app/requirements.txt     # зависимости с точными версиями
Dockerfile               # multi-stage, запуск от непривилегированного пользователя, HEALTHCHECK
compose.yaml             # db + redis + api, сеть, тома, healthchecks
db/init.sql              # схема и индексы
.env.example             # пример переменных окружения
scripts/smoke.sh         # сквозная проверка стека командой curl
tests/                   # интеграционные тесты API (pytest)
```

## Запуск (с нуля)

Нужны Docker и Docker Compose v2.

```bash
git clone <ссылка на репозиторий> && cd <каталог>
cp .env.example .env          # при желании поменяйте POSTGRES_PASSWORD
docker compose up --build -d
docker compose ps             # у всех трёх сервисов должно быть (healthy)
```

Остановка: `docker compose down` (данные сохраняются в томах); полная очистка, включая данные: `docker compose down -v`.

## Проверка работоспособности

### 1. Автоматически

```bash
bash scripts/smoke.sh                       # или bash scripts/smoke.sh http://localhost:8000
# ... OK: все проверки пройдены
```

### 2. Вручную (полный сценарий)

```bash
B=http://localhost:8000
H='Content-Type: application/json'

curl $B/health                                                   # {"status":"ok"}: доступны БД и Redis

# 1) создать группу курса
curl -X POST $B/groups -H "$H" -d '{"name":"ПИ-21 Основы разработки"}'
# {"id":1,"name":"ПИ-21 Основы разработки","created_at":"..."}

# 2) вступить в группу
curl -X POST $B/groups/1/members -H "$H" -d '{"username":"alice"}'
curl -X POST $B/groups/1/members -H "$H" -d '{"username":"bob"}'

# 3) отправить сообщения
curl -X POST $B/groups/1/messages -H "$H" -d '{"author":"alice","text":"Привет всем!"}'
curl -X POST $B/groups/1/messages -H "$H" -d '{"author":"bob","text":"Когда сдаём лабу 7?"}'
curl -X POST $B/groups/1/messages -H "$H" -d '{"author":"alice","text":"В пятницу"}'

# 4) последние N сообщений (старые → новые)
curl "$B/groups/1/messages?limit=2"
# два последних: «Когда сдаём лабу 7?» и «В пятницу»

# 5) список групп пользователя
curl $B/users/alice/groups

# 6) не участник писать не может
curl -i -X POST $B/groups/1/messages -H "$H" -d '{"author":"mallory","text":"hi"}'   # HTTP 403

# 7) online-статус (Redis, TTL 60 с)
curl -X POST $B/users/bob/heartbeat
curl $B/groups/1/online                                          # {"group_id":1,"online":["bob"]}
```

Интерактивная документация: `http://localhost:8000/docs`.

### 3. Что проверить в инфраструктуре

```bash
docker compose ps                       # db, redis, api: healthy
docker compose logs -f api              # логи приложения
docker compose exec redis redis-cli LRANGE group:1:last 0 -1     # закэшированные последние сообщения
docker compose exec db psql -U messenger -d messenger -c "SELECT id, author, text FROM messages;"

# персистентность: данные переживают перезапуск
docker compose down && docker compose up -d
curl "$B/groups/1/messages"             # сообщения на месте (том pgdata)

# БД снаружи недоступна (должно НЕ подключиться):
nc -zv localhost 5432
```

## API

| Метод и путь | Назначение | Коды |
|---|---|---|
| `GET /health` | готовность (БД + Redis) | 200 / 503 |
| `POST /groups` `{name}` | создать группу | 201, 409 (имя занято), 422 |
| `GET /groups` | список групп | 200 |
| `POST /groups/{id}/members` `{username}` | вступить в группу (идемпотентно) | 201, 404 |
| `POST /groups/{id}/messages` `{author, text}` | отправить сообщение (только участник) | 201, 403, 404, 422 |
| `GET /groups/{id}/messages?limit=N` | последние N сообщений (1–200, по умолчанию 20) | 200, 404, 422 |
| `GET /users/{username}/groups` | группы пользователя | 200 |
| `POST /users/{username}/heartbeat` | отметить online (TTL) | 200 |
| `GET /groups/{id}/online` | участники группы онлайн | 200, 404 |

## Как используется Redis

* **Кэш последних сообщений:** при отправке сообщение пишется в PostgreSQL, затем `LPUSH` + `LTRIM` в список
  `group:{id}:last` (длина `LAST_CACHE_SIZE`, по умолчанию 50). Чтение `limit ≤ 50`: если в кэше достаточно записей,
  ответ идёт из Redis, иначе из PostgreSQL, и кэш «прогревается».
* **Online-статус:** `heartbeat` ставит ключ `online:{user}` с TTL `ONLINE_TTL_SECONDS` (60 с); пропал ключ означает пользователь не онлайн.
* **Отказоустойчивость:** если Redis недоступен, отправка и чтение сообщений продолжают работать через PostgreSQL;
  `/health` в этом случае вернёт 503, а `/heartbeat` и `/online` тоже ответят 503.

## Тесты

Интеграционные тесты (17 штук) работают против реальных PostgreSQL и Redis. В `compose.yaml` порты БД и Redis
не опубликованы (так безопаснее), поэтому для тестов поднимите отдельные временные контейнеры:

```bash
docker run -d --name t-pg -e POSTGRES_USER=messenger -e POSTGRES_PASSWORD=testpass -e POSTGRES_DB=messenger \
  -v "$PWD/db/init.sql:/docker-entrypoint-initdb.d/01.sql:ro" -p 5432:5432 postgres:16-alpine
docker run -d --name t-redis -p 6379:6379 redis:7-alpine

pip install -r app/requirements.txt -r tests/requirements.txt
export DATABASE_URL=postgresql://messenger:testpass@localhost:5432/messenger REDIS_URL=redis://localhost:6379/0
pytest tests -v
docker rm -f t-pg t-redis
```

> Тесты вызывают `FLUSHDB` в Redis: запускайте их только на тестовом экземпляре.

## Ограничения (учебный MVP)

* Нет аутентификации: `author` и `username` передаются в теле запроса.
* Соединение с БД открывается на каждый запрос (для продакшена нужен пул соединений).
* При конкурентной отправке порядок в Redis-списке теоретически может отличаться от порядка `id` в БД; источник истины — PostgreSQL.
* Схема из `db/init.sql` применяется только при пустом томе; для изменений схемы нужны миграции (Alembic и т.п.).
