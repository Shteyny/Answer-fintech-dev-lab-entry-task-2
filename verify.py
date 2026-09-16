#!/usr/bin/env python3
"""
Полный сценарий проверки сервиса.

Требует:
  - запущенного docker compose (все три сервиса Up);
  - только стандартной библиотеки Python 3.10+.

Запуск:
  docker compose up --build -d
  python3 verify.py

Exit-код: 0 — всё зелёное, 1 — есть падения.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

BASE = "http://localhost:8080"
TIMEOUT = 10.0


# ============================================================================
# HTTP-хелперы на стандартной библиотеке
# ============================================================================
def _request(method: str, path: str, body: dict | None = None) -> tuple[int, dict | list | None]:
    """
    Возвращает (status_code, parsed_json_body_or_None).
    Не бросает исключений при 4xx/5xx — отдаёт код как есть.
    """
    url = f"{BASE}{path}"
    data = None
    headers = {"Accept": "application/json"}

    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            return resp.status, _parse_json(raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, _parse_json(raw)
    except urllib.error.URLError as e:
        raise RuntimeError(f"HTTP request failed: {method} {url} — {e}") from e


def _parse_json(raw: bytes) -> dict | list | None:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError, UnicodeDecodeError:
        return None


def http_get(path: str) -> tuple[int, dict | list | None]:
    return _request("GET", path)


def http_post(path: str, body: dict | None = None) -> tuple[int, dict | list | None]:
    return _request("POST", path, body)


# ============================================================================
# Docker Compose
# ============================================================================
def docker_compose_logs(service: str) -> str:
    result = subprocess.run(
        ["docker", "compose", "logs", "--no-log-prefix", service],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout


def docker_compose_ps_services() -> list[str]:
    result = subprocess.run(
        ["docker", "compose", "ps", "--format", "{{.Service}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return sorted(line.strip() for line in result.stdout.splitlines() if line.strip())


# ============================================================================
# Доменные хелперы
# ============================================================================
def new_op_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time())}-{uuid.uuid4().hex[:4]}"


def create_operation(op: str, amount: str = "100.00") -> tuple[int, dict | list | None]:
    return http_post(
        "/operations",
        {
            "operationId": op,
            "amount": amount,
            "currency": "RUB",
            "description": "verify",
        },
    )


def submit(op: str) -> tuple[int, dict | list | None]:
    return http_post(f"/operations/{op}/submit")


def get_operation(op: str) -> dict:
    _, body = http_get(f"/operations/{op}")
    assert isinstance(body, dict)
    return body


def get_events(op: str) -> list[dict]:
    _, body = http_get(f"/operations/{op}/events")
    assert isinstance(body, list)
    return body


def send_receipt(
    op: str, ppid: str, result: str, message: str = "verify"
) -> tuple[int, dict | list | None]:
    return http_post(
        "/receipts",
        {
            "providerPaymentId": ppid,
            "operationId": op,
            "result": result,
            "message": message,
            "occurredAt": "2026-01-01T00:00:00Z",
        },
    )


# ============================================================================
# Reporter
# ============================================================================
@dataclass
class Reporter:
    passed: int = 0
    failed: int = 0
    failures: list[str] = field(default_factory=list)

    def section(self, title: str) -> None:
        print()
        print("═" * 68)
        print(f" {title}")
        print("═" * 68)

    def info(self, msg: str) -> None:
        print(f"  · {msg}")

    def check(self, label: str, expected, actual) -> None:
        if expected == actual:
            print(f"  ✅ {label}: {actual}")
            self.passed += 1
        else:
            print(f"  ❌ {label}: expected={expected!r} actual={actual!r}")
            self.failed += 1
            self.failures.append(f"{label}: expected={expected!r} actual={actual!r}")

    def check_true(self, label: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  ✅ {label}{(' — ' + detail) if detail else ''}")
            self.passed += 1
        else:
            print(f"  ❌ {label}{(' — ' + detail) if detail else ''}")
            self.failed += 1
            self.failures.append(f"{label} — {detail}")


# ============================================================================
# Секции проверок
# ============================================================================
def section_0_health(r: Reporter) -> None:
    r.section("0. Health и состояние сервисов")

    code, body = http_get("/health")
    r.check("GET /health — статус", 200, code)
    r.check("GET /health — тело", {"status": "ok"}, body)

    services = docker_compose_ps_services()
    expected = ["candidate-service", "postgres", "provider-simulator"]
    r.check("все три сервиса Up", expected, services)


def section_1_basic_completed(r: Reporter) -> str:
    r.section("1. Базовый путь до COMPLETED через реального провайдера")

    op = new_op_id("op-demo")
    r.info(f"OP={op}")

    code, body = create_operation(op, "1000.00")
    r.check("создание → 201", 201, code)
    assert isinstance(body, dict)
    r.check("начальный статус", "CREATED", body["status"])

    code, _ = create_operation(op, "1000.00")
    r.check("дубль → 409", 409, code)

    code, _ = submit(op)
    r.check("первый submit → 202", 202, code)

    code, _ = submit(op)
    r.check("повторный submit → 200", 200, code)

    r.info("ждём квитанцию 5 секунд")
    time.sleep(5)

    state = get_operation(op)
    r.info(f"финал: {json.dumps(state, ensure_ascii=False)}")
    r.check("статус", "COMPLETED", state["status"])
    r.check_true(
        "providerPaymentId заполнен",
        bool(state["providerPaymentId"]),
        state["providerPaymentId"] or "",
    )

    events = get_events(op)
    r.check("количество событий", 3, len(events))
    r.check(
        "типы событий",
        ["CREATED", "SUBMITTED", "COMPLETED"],
        [e["type"] for e in events],
    )
    r.check("eventId монотонный", [1, 2, 3], [e["eventId"] for e in events])

    return op


def section_2_provider_audit(r: Reporter, op: str) -> None:
    r.section("2. Аудит провайдера — ровно один платёж на операцию")

    logs = docker_compose_logs("provider-simulator")
    op_lines = [line for line in logs.splitlines() if op in line]

    accepted = [l for l in op_lines if '"msg":"payment accepted"' in l]
    delivered = [l for l in op_lines if '"msg":"callback delivered"' in l]
    replay_false = [l for l in op_lines if '"replay":false' in l]
    replay_true = [l for l in op_lines if '"replay":true' in l]

    r.check("payment accepted (первый вызов)", 1, len(accepted))
    r.check("callback delivered", 1, len(delivered))
    r.check("первый вызов с replay:false", 1, len(replay_false))
    r.check("нет replay:true на happy path", 0, len(replay_true))

    unique_ppids = set()
    for line in accepted:
        try:
            payload = json.loads(line)
            unique_ppids.add(payload.get("providerPaymentId"))
        except json.JSONDecodeError:
            pass
    r.check_true(
        "один и тот же ppid во всех вызовах",
        len(unique_ppids) <= 1,
        str(unique_ppids),
    )


def section_3_concurrent_submit(r: Reporter) -> str:
    r.section("3. Конкурентный submit — 10 параллельных запросов")

    op = new_op_id("op-race")
    r.info(f"OP_RACE={op}")

    create_operation(op, "500.00")

    def one_submit(_: int) -> int:
        code, _ = submit(op)
        return code

    with ThreadPoolExecutor(max_workers=10) as pool:
        codes = list(pool.map(one_submit, range(10)))

    r.check("ровно один 202", 1, codes.count(202))
    r.check("девять 200", 9, codes.count(200))

    time.sleep(4)
    events = get_events(op)
    r.check("событий после гонки", 3, len(events))

    return op


def section_4_receipts(r: Reporter, op: str, ppid: str) -> None:
    r.section("4. Дубликат / поздняя / чужая квитанция")

    code, _ = send_receipt(op, ppid, "COMPLETED", "dup")
    r.check("дубликат → 204", 204, code)

    code, _ = send_receipt(op, ppid, "REJECTED", "late")
    r.check("поздняя противоположная → 204", 204, code)

    code, _ = send_receipt(op, "pp-foreign", "COMPLETED", "x")
    r.check("чужой ppid → 409", 409, code)

    code, _ = send_receipt("nonexistent-op-xyz", "pp", "COMPLETED", "x")
    r.check("неизвестная операция → 404", 404, code)

    state = get_operation(op)
    r.check("статус не изменился", "COMPLETED", state["status"])
    r.check("ppid не изменился", ppid, state["providerPaymentId"])

    events = get_events(op)
    r.check("событий всё ещё 3", 3, len(events))


def section_5_rejected(r: Reporter) -> None:
    r.section("5. Путь до REJECTED через ручную квитанцию")

    op = new_op_id("op-rejected")
    r.info(f"OP_REJ={op}")

    create_operation(op, "50.00")

    ppid = f"manual-{int(time.time())}"
    code, _ = send_receipt(op, ppid, "REJECTED", "Declined")
    r.check("квитанция REJECTED → 204", 204, code)

    state = get_operation(op)
    r.check("статус", "REJECTED", state["status"])
    r.check("ppid установлен из квитанции", ppid, state["providerPaymentId"])

    code, _ = submit(op)
    r.check("submit после REJECTED → 200", 200, code)

    events = get_events(op)
    r.check("событий", 2, len(events))
    r.check(
        "типы событий",
        ["CREATED", "REJECTED"],
        [e["type"] for e in events],
    )


def section_6_restart(r: Reporter) -> None:
    r.section("6. Рестарт candidate-service во время PROCESSING")

    op = new_op_id("op-restart")
    r.info(f"OP_RESTART={op}")

    create_operation(op, "777.00")
    code, _ = submit(op)
    r.check("submit перед рестартом → 202", 202, code)

    r.info("рестартим candidate-service")
    subprocess.run(
        ["docker", "compose", "restart", "candidate-service"],
        capture_output=True,
        check=False,
    )
    r.info("ждём 8 секунд")
    time.sleep(8)

    state = get_operation(op)
    events = get_events(op)
    r.info(f"статус={state['status']} ppid={state['providerPaymentId']} events={len(events)}")

    r.check_true(
        "статус допустимый",
        state["status"] in ("COMPLETED", "PROCESSING"),
        state["status"],
    )

    logs = docker_compose_logs("provider-simulator")
    op_lines = [l for l in logs.splitlines() if op in l and '"msg":"payment accepted"' in l]
    unique_ppids = set()
    for line in op_lines:
        try:
            payload = json.loads(line)
            unique_ppids.add(payload.get("providerPaymentId"))
        except json.JSONDecodeError:
            pass
    r.check_true(
        "все вызовы с одним ppid",
        len(unique_ppids) <= 1,
        f"ppids={unique_ppids}",
    )


def section_7_persistence(r: Reporter, op_completed: str, op_race: str) -> None:
    r.section("7. Сохранность данных при docker compose down && up")

    r.info("docker compose down")
    subprocess.run(["docker", "compose", "down"], capture_output=True, check=False)
    time.sleep(3)  # ← дать портам освободиться

    r.info("docker compose up -d")
    subprocess.run(["docker", "compose", "up", "-d"], capture_output=True, check=False)
    time.sleep(10)  # ← дать контейнерам подняться

    state1 = get_operation(op_completed)
    r.check("OP из шага 1 сохранился", "COMPLETED", state1["status"])

    state2 = get_operation(op_race)
    r.check("OP из шага 3 сохранился", "COMPLETED", state2["status"])

    events = get_events(op_completed)
    r.check("история тоже сохранилась", 3, len(events))


def section_8_validation(r: Reporter) -> None:
    r.section("8. Валидация входных данных")

    code, _ = http_post(
        "/operations",
        {"operationId": "bad-1", "amount": "-1.00", "currency": "RUB"},
    )
    r.check("отрицательная сумма → 422", 422, code)

    code, _ = http_post(
        "/operations",
        {"operationId": "bad-2", "amount": "10.00", "currency": "USD"},
    )
    r.check("не-RUB валюта → 422", 422, code)

    code, _ = http_post("/operations", {"amount": "10.00", "currency": "RUB"})
    r.check("без operationId → 422", 422, code)

    code, _ = http_post(
        "/operations",
        {"operationId": "bad-3", "amount": "10.123", "currency": "RUB"},
    )
    r.check("три знака после точки → 422", 422, code)

    code, _ = http_post("/operations/nonexistent-op-xyz/submit")
    r.check("submit несуществующей → 404", 404, code)

    code, _ = http_get("/operations/nonexistent-op-xyz/events")
    r.check("events несуществующей → 404", 404, code)


# ============================================================================
# Main
# ============================================================================
def main() -> int:
    r = Reporter()

    try:
        section_0_health(r)
        op_completed = section_1_basic_completed(r)
        state = get_operation(op_completed)
        ppid = state["providerPaymentId"]

        section_2_provider_audit(r, op_completed)
        op_race = section_3_concurrent_submit(r)
        section_4_receipts(r, op_completed, ppid)
        section_5_rejected(r)
        section_6_restart(r)
        section_7_persistence(r, op_completed, op_race)
        section_8_validation(r)
    except Exception as e:
        r.failed += 1
        r.failures.append(f"unhandled exception: {e}")
        print(f"\n  ⚠️  Unhandled exception: {e}")

    r.section("ИТОГ")
    print(f"  PASS: {r.passed}")
    print(f"  FAIL: {r.failed}")
    print()

    if r.failed == 0:
        print("  🎉 ВСЁ ЗЕЛЁНОЕ")
        return 0

    print("  ⚠️  ПАДЕНИЯ:")
    for f in r.failures:
        print(f"    - {f}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
