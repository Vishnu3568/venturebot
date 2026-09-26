import urllib.request

def check_url(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'VentureBot-Step61-Verifier/1.0'})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read().decode('utf-8')

status_idx, body_idx = check_url('https://vishnu3568.github.io/venturebot/pilot/freelance-workflow/')
status_gd, body_gd = check_url('https://vishnu3568.github.io/venturebot/pilot/freelance-workflow/guide.html')

cta_present = 'href="guide.html"' in body_idx
canon_idx_present = "https://vishnu3568.github.io/venturebot/pilot/freelance-workflow/" in body_idx
vdev_present = "venturebot.dev" in body_idx.lower()
pemail_present = "pilot@venturebot.dev" in body_idx

canon_gd_present = "https://vishnu3568.github.io/venturebot/pilot/freelance-workflow/guide.html" in body_gd
backlink_present = 'href="index.html"' in body_gd
print_present = "window.print()" in body_gd

print('--- LIVE VERIFICATION RESULTS ---')
print(f'Landing Page: HTTP {status_idx}, size: {len(body_idx)} bytes')
print(f'  Canonical present: {canon_idx_present}')
print(f'  CTA href="guide.html" present: {cta_present}')
print(f'  venturebot.dev present: {vdev_present}')
print(f'  pilot@venturebot.dev present: {pemail_present}')

print(f'\nGuide Page: HTTP {status_gd}, size: {len(body_gd)} bytes')
print(f'  Canonical present: {canon_gd_present}')
print(f'  Back link to index.html present: {backlink_present}')
print(f'  Print button present: {print_present}')
for sec in [
    'Single-Source Invoice Log',
    'Predictable Follow-Up Cadence',
    'Receivables Visibility System',
    'Cash-Flow Buffer Organization',
    '15-Minute Weekly Financial Routine'
]:
    present = sec in body_gd
    print(f'  Syllabus "{sec}" present: {present}')
