#!/usr/bin/env python3
"""latest.json 품질 점검 요약 (커밋 전 확인용)
  python scripts/check_latest.py data/latest.json [data/raw]   # raw 폴더를 주면 공시 원문 소유형·대유형 분포도 출력
"""
import glob, json, os, sys
from collections import Counter

d = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "data/latest.json", encoding="utf-8"))
F = d["funds"]
print(f"기준일 {d['base_date']} · 펀드 {len(F)}개 · 출처 {d['source']}")

ids = Counter(f["fund_id"] for f in F)
dup = [k for k, v in ids.items() if v > 1]
print(f"fund_id 중복: {len(dup)}개 {dup[:5]}")

def show(title, cnt):
    print(f"\n[{title}]")
    for k, v in cnt.most_common(): print(f"  {k}: {v}")

show("보험사 (회사코드)", Counter(f"{f['insurer_name']} ({f['fund_id'].split('_')[0]})" for f in F))
show("fund_type (소유형 매핑 결과)", Counter(f["fund_type"] for f in F))
show("region (대유형 매핑 결과)", Counter(str(f["region"]) for f in F))

print("\n[수익률 값 있음 / 음수 개수]")
for p in ["1D", "1W", "1M", "3M", "6M", "YTD", "1Y", "3Y", "5Y"]:
    v = [f["ret"][p] for f in F if f["ret"][p] is not None]
    print(f"  {p:>3}: {len(v):>5}개 · 음수 {sum(x < 0 for x in v):>4}개" + (f" · 범위 {min(v):.2f} ~ {max(v):.2f}" if v else ""))

na = [f for f in F if f["net_assets_100m"] is not None and f["net_assets_100m"] < 10]
print(f"\n순자산 10억 미만(기본 숨김 대상): {len(na)}개")
print(f"순자산 합계: {sum(f['net_assets_100m'] or 0 for f in F):,.0f}억 원")

neg = sorted([f for f in F if (f["ret"]["1Y"] or 0) < 0], key=lambda f: f["ret"]["1Y"])[:3]
print("\n[1Y 음수 예시 — 공시실 화면과 대조]")
for f in neg: print(f"  {f['insurer_name']} {f['fund_name']} 1Y {f['ret']['1Y']} 3Y {f['ret']['3Y']}")
print("\n[샘플 3개]")
for f in F[:3]: print(" ", json.dumps({k: f[k] for k in ['fund_id','fund_name','fund_type','region','nav','net_assets_100m','total_fee_pct','ret']}, ensure_ascii=False))

if len(sys.argv) > 2 and os.path.isdir(sys.argv[2]):
    import pandas as pd
    raw = pd.concat([pd.read_csv(f, dtype=str, keep_default_na=False) for f in glob.glob(os.path.join(sys.argv[2], "nav_*.csv*"))])
    raw = raw[raw["기준일"] == raw["기준일"].max()]
    print(f"\n[공시 원문 대유형 × 소유형] (기준일 {raw['기준일'].max()}, {len(raw)}행)")
    print(pd.crosstab(raw["소유형"], raw["대유형"], margins=True, margins_name="합계").to_string())
