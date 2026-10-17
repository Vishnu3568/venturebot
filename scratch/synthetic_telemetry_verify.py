"""Synthetic verification of the deployed Cloudflare Worker + D1 telemetry infrastructure.

Runs 12 controlled tests against https://venturebot-telemetry.uvishnu3568.workers.dev
using isolated synthetic test UUIDs.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from uuid import uuid4

# Add backend to path to import VentureBot models
sys.path.insert(0, os.path.abspath("backend"))

from venturebot.env import load_env_file
from venturebot.models.evidence import EvidenceCategory
from venturebot.measurement.guide_telemetry import (
    GuideAccessTelemetrySummary,
    build_canonical_guide_access_source_reference,
)

load_env_file()

ENDPOINT = os.environ.get("VENTUREBOT_TELEMETRY_ENDPOINT", "").rstrip("/")
SECRET = os.environ.get("VENTUREBOT_TELEMETRY_RETRIEVAL_KEY", "")

assert ENDPOINT, "VENTUREBOT_TELEMETRY_ENDPOINT is not set in .env"
assert SECRET, "VENTUREBOT_TELEMETRY_RETRIEVAL_KEY is not set in .env"

TEST_EXP_A = str(uuid4())
TEST_EXP_B = str(uuid4())
TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

UA = "VentureBot-Telemetry-Synthetic/1.0"

print(f"Testing endpoint: {ENDPOINT}")
print(f"Synthetic Exp A: {TEST_EXP_A}")
print(f"Synthetic Exp B: {TEST_EXP_B}")
print(f"UTC Date: {TODAY}")

results = []

def record(name: str, passed: bool, detail: str = ""):
    results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    print(f"[{status}] {name} {detail}")

# ── 1. GET without Authorization -> 401 ──────────────────────────────────────
try:
    url = f"{ENDPOINT}/api/v1/telemetry/summary?experiment_id={TEST_EXP_A}&date_start={TODAY}&date_stop={TODAY}"
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method="GET")
    with urllib.request.urlopen(req) as resp:
        record("GET without Authorization -> 401", False, f"Unexpected code {resp.status}")
except urllib.error.HTTPError as e:
    record("GET without Authorization -> 401", e.code == 401, f"HTTP {e.code}")

# ── 2. GET with invalid Authorization -> 401 ────────────────────────────────
try:
    url = f"{ENDPOINT}/api/v1/telemetry/summary?experiment_id={TEST_EXP_A}&date_start={TODAY}&date_stop={TODAY}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Authorization": "Bearer bad-token-1234"}, method="GET")
    with urllib.request.urlopen(req) as resp:
        record("GET with invalid Authorization -> 401", False, f"Unexpected code {resp.status}")
except urllib.error.HTTPError as e:
    record("GET with invalid Authorization -> 401", e.code == 401, f"HTTP {e.code}")

# ── 3. POST with invalid UUID -> 400 ─────────────────────────────────────────
try:
    url = f"{ENDPOINT}/event/guide_access"
    payload = json.dumps({"experiment_id": "not-a-valid-uuid", "event_type": "guide_access"}).encode()
    req = urllib.request.Request(url, data=payload, headers={"User-Agent": UA, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        record("POST invalid UUID -> 400", False, f"Unexpected code {resp.status}")
except urllib.error.HTTPError as e:
    record("POST invalid UUID -> 400", e.code == 400, f"HTTP {e.code}")

# ── 4. POST with invalid event_type -> 400 ───────────────────────────────────
try:
    url = f"{ENDPOINT}/event/guide_access"
    payload = json.dumps({"experiment_id": TEST_EXP_A, "event_type": "page_view"}).encode()
    req = urllib.request.Request(url, data=payload, headers={"User-Agent": UA, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        record("POST invalid event_type -> 400", False, f"Unexpected code {resp.status}")
except urllib.error.HTTPError as e:
    record("POST invalid event_type -> 400", e.code == 400, f"HTTP {e.code}")

# ── 5. POST with oversized payload (>1024 bytes) -> 413 ─────────────────────
try:
    url = f"{ENDPOINT}/event/guide_access"
    oversized = {"experiment_id": TEST_EXP_A, "event_type": "guide_access", "padding": "x" * 2000}
    payload = json.dumps(oversized).encode()
    req = urllib.request.Request(url, data=payload, headers={"User-Agent": UA, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        record("POST oversized payload -> 413", False, f"Unexpected code {resp.status}")
except urllib.error.HTTPError as e:
    record("POST oversized payload -> 413", e.code == 413, f"HTTP {e.code}")

# ── 6. Initial GET for Exp A with valid auth -> 200, count == 0 ──────────────
url = f"{ENDPOINT}/api/v1/telemetry/summary?experiment_id={TEST_EXP_A}&date_start={TODAY}&date_stop={TODAY}"
req = urllib.request.Request(url, headers={"User-Agent": UA, "Authorization": f"Bearer {SECRET}"}, method="GET")
with urllib.request.urlopen(req) as resp:
    data = json.loads(resp.read().decode())
    passed = resp.status == 200 and data.get("count") == 0
    record("Initial GET with valid auth -> 200, count == 0", passed, f"count={data.get('count')}")

# ── 7. POST valid event -> 204 with CORS header ─────────────────────────────
url = f"{ENDPOINT}/event/guide_access"
payload = json.dumps({"experiment_id": TEST_EXP_A, "event_type": "guide_access"}).encode()
req = urllib.request.Request(
    url,
    data=payload,
    headers={
        "User-Agent": UA,
        "Content-Type": "application/json",
        "Origin": "https://vishnu3568.github.io",
    },
    method="POST",
)
with urllib.request.urlopen(req) as resp:
    cors = resp.headers.get("Access-Control-Allow-Origin")
    passed = resp.status == 204 and cors == "https://vishnu3568.github.io"
    record("POST valid event -> 204 with CORS", passed, f"status={resp.status}, cors={cors}")

# ── 8. GET after 1 POST -> count == 1 ────────────────────────────────────────
url = f"{ENDPOINT}/api/v1/telemetry/summary?experiment_id={TEST_EXP_A}&date_start={TODAY}&date_stop={TODAY}"
req = urllib.request.Request(url, headers={"User-Agent": UA, "Authorization": f"Bearer {SECRET}"}, method="GET")
with urllib.request.urlopen(req) as resp:
    data = json.loads(resp.read().decode())
    passed = data.get("count") == 1
    record("GET after 1 POST -> count == 1", passed, f"count={data.get('count')}")

# ── 9. Repeated GET does not mutate count ────────────────────────────────────
req = urllib.request.Request(url, headers={"User-Agent": UA, "Authorization": f"Bearer {SECRET}"}, method="GET")
with urllib.request.urlopen(req) as resp:
    data = json.loads(resp.read().decode())
    passed = data.get("count") == 1
    record("Repeated GET does not mutate count", passed, f"count={data.get('count')}")

# ── 10. Multiple POSTs produce expected count (send 2 more -> count == 3) ───
for _ in range(2):
    url = f"{ENDPOINT}/event/guide_access"
    payload = json.dumps({"experiment_id": TEST_EXP_A, "event_type": "guide_access"}).encode()
    req = urllib.request.Request(url, data=payload, headers={"User-Agent": UA, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        pass

url = f"{ENDPOINT}/api/v1/telemetry/summary?experiment_id={TEST_EXP_A}&date_start={TODAY}&date_stop={TODAY}"
req = urllib.request.Request(url, headers={"User-Agent": UA, "Authorization": f"Bearer {SECRET}"}, method="GET")
with urllib.request.urlopen(req) as resp:
    data = json.loads(resp.read().decode())
    passed = data.get("count") == 3
    record("Multiple POSTs produce expected count (3)", passed, f"count={data.get('count')}")

# ── 11. Experiment Isolation: Exp B count remains 0 ──────────────────────────
url = f"{ENDPOINT}/api/v1/telemetry/summary?experiment_id={TEST_EXP_B}&date_start={TODAY}&date_stop={TODAY}"
req = urllib.request.Request(url, headers={"User-Agent": UA, "Authorization": f"Bearer {SECRET}"}, method="GET")
with urllib.request.urlopen(req) as resp:
    data = json.loads(resp.read().decode())
    passed = data.get("count") == 0
    record("Experiment isolation (Exp B count == 0)", passed, f"count={data.get('count')}")

# ── 12. Contract compatibility: Parse response into GuideAccessTelemetrySummary
summary = GuideAccessTelemetrySummary.model_validate(data)
expected_ref = build_canonical_guide_access_source_reference(TEST_EXP_B, date.fromisoformat(TODAY), date.fromisoformat(TODAY))
passed = summary.source_reference == expected_ref and summary.evidence_type == EvidenceCategory.FACT
record("VentureBot GuideAccessTelemetrySummary contract match", passed, f"source_ref={summary.source_reference}")

print("\n--- SYNTHETIC VERIFICATION SUMMARY ---")
all_passed = all(p for _, p, _ in results)
print(f"Total tests: {len(results)} | Passed: {sum(1 for _, p, _ in results if p)} | Failed: {sum(1 for _, p, _ in results if not p)}")

if not all_passed:
    sys.exit(1)
