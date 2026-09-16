#!/usr/bin/env bash
# Полный сценарий проверки сервиса.
# Требует: bash, curl, запущенный docker compose.
#
# Запуск:
#   docker compose up --build -d
#   bash verify.sh
#
# Exit-код: 0 — всё зелёное, 1 — есть падения.

set -u

BASE=http://localhost:8080
PASS=0
FAIL=0
FAILURES=()

# ============================================================================
# Хелперы вывода
# ============================================================================
section() {
  echo
  echo "════════════════════════════════════════════════════════════════"
  echo " $1"
  echo "════════════════════════════════════════════════════════════════"
}

info() { echo "  · $1"; }

check() {
  local label="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    echo "  ✅ $label: $actual"
    PASS=$((PASS + 1))
  else
    echo "  ❌ $label: expected=$expected actual=$actual"
    FAIL=$((FAIL + 1))
    FAILURES+=("$label: expected=$expected actual=$actual")
  fi
}

check_true() {
  local label="$1" condition="$2" detail="${3:-}"
  if [ "$condition" = "1" ]; then
    echo "  ✅ $label${detail:+ — $detail}"
    PASS=$((PASS + 1))
  else
    echo "  ❌ $label${detail:+ — $detail}"
    FAIL=$((FAIL + 1))
    FAILURES+=("$label — $detail")
  fi
}

# ============================================================================
# HTTP + JSON хелперы (без Python)
# ============================================================================
http_get_code()  { curl -s -o /dev/null -w "%{http_code}" "$BASE$1"; }
http_get_body()  { curl -s "$BASE$1"; }
http_post_code() {
  local body="${2:-}"
  if [ -z "$body" ]; then
    curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE$1"
  else
    curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE$1" \
      -H 'Content-Type: application/json' -d "$body"
  fi
}
http_post_body() {
  curl -s -X POST "$BASE$1" -H 'Content-Type: application/json' -d "$2"
}

# Извлечь значение поля (строку/число/null) из простого JSON.
jget() {
  local body="$1" key="$2"
  echo "$body" \
    | grep -oE "\"$key\"[[:space:]]*:[[:space:]]*(\"[^\"]*\"|null|true|false|-?[0-9.]+)" \
    | head -1 \
    | sed -E "s/^\"$key\"[[:space:]]*:[[:space:]]*//; s/^\"//; s/\"$//"
}

# Посчитать количество вхождений ключа.
jcount() {
  local body="$1" key="$2"
  echo "$body" | grep -oE "\"$key\"" | wc -l | tr -d ' '
}

# Извлечь последовательность "type":"X" в порядке появления.
jtypes() {
  echo "$1" | grep -oE '"type"[[:space:]]*:[[:space:]]*"[A-Z]+"' \
    | sed -E 's/.*"([A-Z]+)".*/\1/' | paste -sd, -
}

new_op() {
  echo "$1-$(date +%s)-$RANDOM"
}

create_op() {
  local op="$1" amount="${2:-100.00}"
  http_post_body "/operations" \
    "{\"operationId\":\"$op\",\"amount\":\"$amount\",\"currency\":\"RUB\",\"description\":\"verify\"}"
}

submit_op() {
  http_post_code "/operations/$1/submit"
}

get_op() {
  http_get_body "/operations/$1"
}

get_events() {
  http_get_body "/operations/$1/events"
}

send_receipt() {
  local op="$1" ppid="$2" result="$3" msg="${4:-verify}"
  http_post_code "/receipts" \
    "{\"providerPaymentId\":\"$ppid\",\"operationId\":\"$op\",\"result\":\"$result\",\"message\":\"$msg\",\"occurredAt\":\"2026-01-01T00:00:00Z\"}"
}

# ============================================================================
# 0. Health
# ============================================================================
section "0. Health и состояние сервисов"

CODE=$(http_get_code "/health")
BODY=$(http_get_body "/health")
check "GET /health — статус" "200" "$CODE"
check "GET /health — тело" '{"status":"ok"}' "$BODY"

SERVICES=$(docker compose ps --format '{{.Service}}' 2>/dev/null | sort | paste -sd, -)
check "все три сервиса Up" "candidate-service,postgres,provider-simulator" "$SERVICES"

# ============================================================================
# 1. Базовый путь до COMPLETED
# ============================================================================
section "1. Базовый путь до COMPLETED через реального провайдера"

OP=$(new_op op-demo)
info "OP=$OP"

CREATE_BODY=$(create_op "$OP" "1000.00")
STATUS=$(jget "$CREATE_BODY" status)
check "начальный статус" "CREATED" "$STATUS"

DUP=$(http_post_code "/operations" "{\"operationId\":\"$OP\",\"amount\":\"1000.00\",\"currency\":\"RUB\"}")
check "дубль → 409" "409" "$DUP"

SUB1=$(submit_op "$OP")
check "первый submit → 202" "202" "$SUB1"

SUB2=$(submit_op "$OP")
check "повторный submit → 200" "200" "$SUB2"

info "ждём квитанцию 5 секунд"
sleep 5

FINAL=$(get_op "$OP")
STATUS=$(jget "$FINAL" status)
PAY_ID=$(jget "$FINAL" providerPaymentId)
info "финал: status=$STATUS ppid=$PAY_ID"
check "статус" "COMPLETED" "$STATUS"
check_true "providerPaymentId заполнен" "$([ -n "$PAY_ID" ] && echo 1 || echo 0)" "$PAY_ID"

EVENTS=$(get_events "$OP")
COUNT=$(jcount "$EVENTS" eventId)
TYPES=$(jtypes "$EVENTS")
check "количество событий" "3" "$COUNT"
check "типы событий" "CREATED,SUBMITTED,COMPLETED" "$TYPES"

# ============================================================================
# 2. Аудит провайдера
# ============================================================================
section "2. Аудит провайдера — ровно один платёж на операцию"

PROV_LOGS=$(docker compose logs --no-log-prefix provider-simulator 2>/dev/null | grep "$OP" || true)
PAY_ACCEPTED=$(echo "$PROV_LOGS" | grep -c '"msg":"payment accepted"' || true)
CALLBACK_OK=$(echo "$PROV_LOGS" | grep -c '"msg":"callback delivered"' || true)
REPLAY_FALSE=$(echo "$PROV_LOGS" | grep -c '"replay":false' || true)
REPLAY_TRUE=$(echo "$PROV_LOGS" | grep -c '"replay":true' || true)

check "payment accepted" "1" "$PAY_ACCEPTED"
check "callback delivered" "1" "$CALLBACK_OK"
check "первый вызов с replay:false" "1" "$REPLAY_FALSE"
check "нет replay:true на happy path" "0" "$REPLAY_TRUE"

# ============================================================================
# 3. Конкурентный submit
# ============================================================================
section "3. Конкурентный submit — 10 параллельных запросов"

OP_RACE=$(new_op op-race)
info "OP_RACE=$OP_RACE"
create_op "$OP_RACE" "500.00" >/dev/null

CODES_FILE=$(mktemp)
for _ in $(seq 1 10); do
  ( curl -s -o /dev/null -w "%{http_code}\n" \
      -X POST "$BASE/operations/$OP_RACE/submit" >> "$CODES_FILE" ) &
done
wait

COUNT_202=$(grep -c '^202$' "$CODES_FILE" || true)
COUNT_200=$(grep -c '^200$' "$CODES_FILE" || true)
check "ровно один 202" "1" "$COUNT_202"
check "девять 200" "9" "$COUNT_200"
rm -f "$CODES_FILE"

sleep 4
EVENTS_RACE=$(get_events "$OP_RACE")
RACE_COUNT=$(jcount "$EVENTS_RACE" eventId)
check "событий после гонки" "3" "$RACE_COUNT"

# ============================================================================
# 4. Дубликат / поздняя / чужая
# ============================================================================
section "4. Дубликат / поздняя / чужая квитанция"

R=$(send_receipt "$OP" "$PAY_ID" "COMPLETED" "dup")

R=$(send_receipt "$OP" "$PAY_ID" "REJECTED" "late")

R=$(send_receipt "$OP" "pp-foreign" "COMPLETED" "x")
check "чужой ppid → 409" "409" "$R"

R=$(send_receipt "nonexistent-op-xyz" "pp" "COMPLETED" "x")
check "неизвестная операция → 404" "404" "$R"

STILL=$(jget "$(get_op "$OP")" status)
check "статус не изменился" "COMPLETED" "$STILL"

EV_COUNT=$(jcount "$(get_events "$OP")" eventId)
check "событий всё ещё 3" "3" "$EV_COUNT"

# ============================================================================
# 5. REJECTED
# ============================================================================
section "5. Путь до REJECTED через ручную квитанцию"

OP_REJ=$(new_op op-rejected)
info "OP_REJ=$OP_REJ"
create_op "$OP_REJ" "50.00" >/dev/null

PPID_REJ="manual-$(date +%s)"
R=$(send_receipt "$OP_REJ" "$PPID_REJ" "REJECTED" "Declined")
check "квитанция REJECTED → 204" "204" "$R"

REJ_STATUS=$(jget "$(get_op "$OP_REJ")" status)
REJ_PPID=$(jget "$(get_op "$OP_REJ")" providerPaymentId)
check "статус" "REJECTED" "$REJ_STATUS"
check "ppid установлен из квитанции" "$PPID_REJ" "$REJ_PPID"

R=$(submit_op "$OP_REJ")
check "submit после REJECTED → 200" "200" "$R"

REJ_EV=$(jcount "$(get_events "$OP_REJ")" eventId)
REJ_TYPES=$(jtypes "$(get_events "$OP_REJ")")
check "событий" "2" "$REJ_EV"
check "типы событий" "CREATED,REJECTED" "$REJ_TYPES"

# ============================================================================
# 6. Рестарт
# ============================================================================
section "6. Рестарт candidate-service во время PROCESSING"

OP_RST=$(new_op op-restart)
info "OP_RESTART=$OP_RST"
create_op "$OP_RST" "777.00" >/dev/null

R=$(submit_op "$OP_RST")
check "submit перед рестартом → 202" "202" "$R"

info "рестартим candidate-service"
docker compose restart candidate-service >/dev/null 2>&1
info "ждём 8 секунд"
sleep 8

RST_STATUS=$(jget "$(get_op "$OP_RST")" status)
RST_PPID=$(jget "$(get_op "$OP_RST")" providerPaymentId)
info "статус=$RST_STATUS ppid=$RST_PPID"

if [ "$RST_STATUS" = "COMPLETED" ] || [ "$RST_STATUS" = "PROCESSING" ]; then
  check_true "статус допустимый" "1" "$RST_STATUS"
else
  check_true "статус допустимый" "0" "$RST_STATUS"
fi

RST_PAY=$(docker compose logs --no-log-prefix provider-simulator 2>/dev/null \
  | grep "$OP_RST" | grep -c '"msg":"payment accepted"' || true)
UNIQ_PPIDS=$(docker compose logs --no-log-prefix provider-simulator 2>/dev/null \
  | grep "$OP_RST" | grep -oE '"providerPaymentId":"[^"]*"' | sort -u | wc -l | tr -d ' ')
check_true "все вызовы с одним ppid" "$([ "$UNIQ_PPIDS" -le 1 ] && echo 1 || echo 0)" "уникальных=$UNIQ_PPIDS"

# ============================================================================
# 7. Сохранность данных
# ============================================================================
section "7. Сохранность данных при docker compose down && up"

info "docker compose down"
docker compose down >/dev/null 2>&1
info "docker compose up -d"
docker compose up -d >/dev/null 2>&1
info "ждём 6 секунд"
sleep 6

P1=$(jget "$(get_op "$OP")" status)
P2=$(jget "$(get_op "$OP_RACE")" status)
check "OP из шага 1 сохранился" "COMPLETED" "$P1"
check "OP_RACE из шага 3 сохранился" "COMPLETED" "$P2"

P_EV=$(jcount "$(get_events "$OP")" eventId)
check "история тоже сохранилась" "3" "$P_EV"

# ============================================================================
# 8. Валидация
# ============================================================================
section "8. Валидация входных данных"

R=$(http_post_code "/operations" '{"operationId":"bad-1","amount":"-1.00","currency":"RUB"}')
check "отрицательная сумма → 422" "422" "$R"

R=$(http_post_code "/operations" '{"operationId":"bad-2","amount":"10.00","currency":"USD"}')
check "не-RUB валюта → 422" "422" "$R"

R=$(http_post_code "/operations" '{"amount":"10.00","currency":"RUB"}')
check "без operationId → 422" "422" "$R"

R=$(http_post_code "/operations" '{"operationId":"bad-3","amount":"10.123","currency":"RUB"}')
check "три знака после точки → 422" "422" "$R"

R=$(http_post_code "/operations/nonexistent-op-xyz/submit" "")
check "submit несуществующей → 404" "404" "$R"

R=$(http_get_code "/operations/nonexistent-op-xyz/events")
check "events несуществующей → 404" "404" "$R"

# ============================================================================
# Итог
# ============================================================================
section "ИТОГ"
echo "  PASS: $PASS"
echo "  FAIL: $FAIL"
echo

if [ "$FAIL" -eq 0 ]; then
  echo "  🎉 ВСЁ ЗЕЛЁНОЕ"
  exit 0
fi

echo "  ⚠️  ПАДЕНИЯ:"
for f in "${FAILURES[@]}"; do
  echo "    - $f"
done
exit 1