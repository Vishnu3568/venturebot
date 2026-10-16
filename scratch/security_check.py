import os
import re

files = [
    ".gitignore",
    "backend/.gitignore",
    "frontend/.gitignore",
    "backend/venturebot/models/metrics.py",
    "backend/venturebot/database/models.py",
    "backend/venturebot/database/repositories/metrics.py",
    "backend/venturebot/measurement/service.py",
    "backend/venturebot/measurement/__init__.py",
    "backend/venturebot/measurement/guide_telemetry.py",
    "backend/venturebot/analysis/service.py",
    "tests/test_guide_telemetry_contract.py",
    "pilot/freelance-workflow/guide.html",
    "docs/pilot/freelance-workflow/guide.html",
    "workers/telemetry/wrangler.toml",
    "workers/telemetry/schema.sql",
    "workers/telemetry/src/index.js",
]

patterns = [
    re.compile(r"TELEMETRY_RETRIEVAL_API_KEY\s*=\s*['\"][^'\"]+"),
    re.compile(r"VENTUREBOT_TELEMETRY_RETRIEVAL_KEY\s*=\s*['\"][^'\"]+"),
    re.compile(r"CLOUDFLARE_API_TOKEN\s*=\s*['\"][^'\"]+"),
    re.compile(r"EAAPHY[a-zA-Z0-9]+"),
    re.compile(r"Bearer\s+[a-f0-9]{32,64}"),
]

found = False
for f in files:
    if not os.path.exists(f):
        print(f"File missing: {f}")
        continue
    content = open(f, "r", encoding="utf-8", errors="ignore").read()
    for p in patterns:
        m = p.search(content)
        if m:
            print(f"SECURITY ALERT: Pattern matched in {f}!")
            found = True

if not found:
    print("SECURITY CHECK PASSED: Zero secret values found in intended files.")
