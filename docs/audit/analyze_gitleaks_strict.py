import json
from collections import Counter

with open('docs/audit/baseline/gitleaks_strict.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

print(f"Total findings in git history: {len(data)}")

rules = Counter(d.get('RuleID') for d in data)
print("\nTop Rules:")
for r, c in rules.most_common(15):
    print(f"  {r}: {c}")

files = Counter(d.get('File') for d in data)
print(f"\nTop 15 Files with leaks across history:")
for fl, c in files.most_common(15):
    print(f"  {fl}: {c}")

tg_leaks = [d for d in data if 'telegram' in d.get('RuleID', '').lower()]
print(f"\nTelegram-bot-token rule findings: {len(tg_leaks)}")
seen_tg = set()
for tg in tg_leaks:
    k = (tg.get('Commit', '')[:8], tg.get('File', ''), tg.get('StartLine', 0))
    if k not in seen_tg:
        seen_tg.add(k)
        print(f"  Commit: {k[0]} | File: {k[1]}:{k[2]}")
