import urllib.request
import json

url = "https://venturebot-telemetry.uvishnu3568.workers.dev/event/guide_access"
payload = json.dumps({"experiment_id": "49fde874-9387-5056-934c-51a9cfca164f", "event_type": "guide_access"}).encode("utf-8")
req = urllib.request.Request(
    url,
    data=payload,
    headers={
        "User-Agent": "VentureBot/1.0",
        "Content-Type": "text/plain",
        "Origin": "https://vishnu3568.github.io"
    },
    method="POST"
)
with urllib.request.urlopen(req) as resp:
    print("STATUS:", resp.status)
    print("CORS:", resp.headers.get("Access-Control-Allow-Origin"))
