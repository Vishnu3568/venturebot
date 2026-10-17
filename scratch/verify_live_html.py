import urllib.request

url = "https://vishnu3568.github.io/venturebot/pilot/freelance-workflow/guide.html"
req = urllib.request.Request(url, headers={"User-Agent": "VentureBot-Verify/1.0", "Cache-Control": "no-cache"})
with urllib.request.urlopen(req) as resp:
    html = resp.read().decode("utf-8")
    status = resp.status

assert status == 200, f"Expected 200, got {status}"
assert "venturebot-telemetry.uvishnu3568.workers.dev" in html, "Endpoint missing from live HTML"
assert "49fde874-9387-5056-934c-51a9cfca164f" in html, "Experiment ID missing from live HTML"
assert "guide_access" in html, "guide_access missing from live HTML"
assert "Single-Source Invoice Log" in html, "Syllabus section missing"
assert "15-Minute Weekly Financial Routine" in html, "Syllabus section missing"
assert '<link rel="canonical" href="https://vishnu3568.github.io/venturebot/pilot/freelance-workflow/guide.html">' in html, "Canonical tag missing"

print("LIVE HTML INTEGRITY CONFIRMED: Status 200, beacon parameters verified, content intact.")
