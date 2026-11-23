#!/usr/bin/env bash
# Сквозная проверка стека: bash scripts/smoke.sh [BASE_URL]
# Падает (exit 1) при первом несовпадении.
set -euo pipefail
BASE="${1:-http://localhost:8000}"
J='Content-Type: application/json'
fail() { echo "FAIL: $*" >&2; exit 1; }
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

NAME="smoke-$(date +%s)"
echo "1. health";                 [ "$(code "$BASE/health")" = 200 ] || fail health
echo "2. создать группу";         GID=$(curl -sf -X POST "$BASE/groups" -H "$J" -d "{\"name\":\"$NAME\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
echo "3. вступить alice, bob";    for u in alice bob; do curl -sf -X POST "$BASE/groups/$GID/members" -H "$J" -d "{\"username\":\"$u\"}" >/dev/null; done
echo "4. отправить сообщения";    for t in "Привет" "Когда дедлайн?" "В пятницу"; do curl -sf -X POST "$BASE/groups/$GID/messages" -H "$J" -d "{\"author\":\"alice\",\"text\":\"$t\"}" >/dev/null; done
echo "5. последние 2";            N=$(curl -sf "$BASE/groups/$GID/messages?limit=2" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(len(d),d[-1]["text"])')
[ "$N" = "2 В пятницу" ] || fail "ожидали '2 В пятницу', получили '$N'"
echo "6. не участник -> 403";     [ "$(code -X POST "$BASE/groups/$GID/messages" -H "$J" -d '{"author":"mallory","text":"hi"}')" = 403 ] || fail 403
echo "7. группы пользователя";    curl -sf "$BASE/users/alice/groups" | grep -q "$NAME" || fail user-groups
echo "8. online (Redis, TTL)";    curl -sf -X POST "$BASE/users/bob/heartbeat" >/dev/null; curl -sf "$BASE/groups/$GID/online" | grep -q '"bob"' || fail online
echo "OK: все проверки пройдены"
