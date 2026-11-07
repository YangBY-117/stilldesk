"""Render the real browser test ledger as an annotated visualization, not video.

Usage: python scripts/render-verification-gif.py
Requires Pillow (provided by the optional messaging test environment).
"""
from pathlib import Path
import json
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
record = json.loads((ROOT / 'tests/browser/e2e-chrome.json').read_text(encoding='utf-8'))
tests = record.get('tests', [])
if not record.get('passed') or not tests:
    raise SystemExit('A passed real browser record is required; no invented test output.')
selected = []
for words in [('foreground',), ('HTTP upload', 'hidden'), ('download',), ('cross-origin',), ('SHA',), ('UID',)]:
    candidate = next((t for t in tests if t.get('passed') and all(w.lower() in t['name'].lower() for w in words)), None)
    if candidate and candidate['name'] not in [t['name'] for t in selected]:
        selected.append(candidate)
for t in tests:
    if len(selected) >= 6:
        break
    if t.get('passed') and t['name'] not in [v['name'] for v in selected]:
        selected.append(t)

def font(size):
    for candidate in ['C:/Windows/Fonts/consola.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf']:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            pass
    return ImageFont.load_default()

frames = []
for count in range(len(selected) + 2):
    im = Image.new('RGB', (1120, 510), '#101411')
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((1, 1, 1118, 508), radius=16, outline='#344332', width=2)
    d.line((1, 56, 1119, 56), fill='#344332', width=1)
    d.text((27, 19), 'stilldesk / verified browser fixture checks', font=font(16), fill='#c7f38a')
    d.text((27, 82), 'REAL TEST LEDGER  /  ANIMATED VISUALIZATION', font=font(12), fill='#788d70')
    for index, item in enumerate(selected[:count]):
        text = item['name']
        if len(text) > 78:
            text = text[:75] + '...'
        d.text((29, 129 + index * 42), '+', font=font(19), fill='#c7f38a')
        d.text((59, 129 + index * 42), text, font=font(16), fill='#dce9d2')
    if count > len(selected):
        d.text((29, 402), f"{len(tests)}/{len(tests)} Chrome fixture checks passed", font=font(20), fill='#c7f38a')
    d.text((29, 465), 'Native Messaging. No remote debugging. No physical input takeover.', font=font(14), fill='#90a584')
    frames.append(im)

output = ROOT / 'docs/assets/verification.gif'
frames[0].save(output, save_all=True, append_images=frames[1:], loop=0,
               duration=[500] + [900] * (len(frames) - 2) + [2600], optimize=True)
print(json.dumps({'path': 'docs/assets/verification.gif', 'frames': len(frames),
                  'kind': 'animated visualization of committed real test ledger; not a screen recording'}))
