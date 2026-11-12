#!/usr/bin/env python3
"""
생명보험협회 공시실 엑셀 → latest.json (data_schema.md 기준) 변환기

사용법:
    python build_latest.py 공시실.xlsx -o data/latest.json [--sheet 시트명] [--header-row 0]
    python build_latest.py 공시실.xlsx --inspect      # 컬럼 매핑 결과만 출력하고 종료
    python build_latest.py data/raw -o data/latest.json     # 수집기 월별 누적분(폴더)

입력 형태 2가지를 자동 판별한다.
  A) 이력형: 펀드×기준일 여러 행 (기준가 시계열) → 기간수익률을 직접 계산
  B) 스냅샷형: 펀드당 1행, 공시 수익률 컬럼 포함 → 해당 컬럼을 그대로 사용

※ COLUMN_ALIASES / INSURER_CODES / FUND_TYPE_RULES 는 실제 공시실 엑셀을 보고 채워야 한다(확인 필요).
"""
import argparse, glob, hashlib, json, os, re, sys
from datetime import date
import pandas as pd

# ───────────────────────── 1. 컬럼 매핑 (확인 필요: 실제 헤더 보고 보완) ─────────────────────────
# 표준 필드 → 헤더 후보(공백·정렬안내문 제거 후 비교). 2026-11-12 공시실 펀드현황(일별) probe 결과 반영
COLUMN_ALIASES = {
    "insurer_name":    ["보험사명", "보험사", "보험회사", "회사명"],
    "insurer_code":    ["회사코드"],          # 협회 회사코드(L01 등), 수집기가 체크박스 value에서 추출
    "fund_code":       ["펀드코드", "펀드번호", "코드"],
    "fund_name":       ["펀드명", "특별계정명", "펀드이름"],
    "fund_type_raw":   ["소유형", "펀드유형", "유형", "펀드분류", "투자유형"],
    "region_raw":      ["대유형", "투자지역", "지역", "국내외구분"],
    "sub_type":        ["세부유형", "소분류"],
    "manager":         ["운용사", "자산운용사", "위탁운용사"],
    "inception_date":  ["설정일", "최초설정일", "설정일자"],
    "total_fee_pct":   ["보수정보(%)_합계", "총보수", "총보수(%)", "보수합계", "총보수율"],
    "product_group":   ["상품군", "상품유형", "상품구분"],
    "is_active":       ["운용여부", "운용상태"],
    "base_date":       ["기준일", "기준일자", "일자"],
    "nav":             ["기준가격(원)", "기준가", "기준가격", "기준가(원)"],
    "net_assets_100m": ["순자산액(억원)", "순자산", "순자산액", "순자산(억원)", "순자산총액"],
    "set_amount_100m": ["설정액", "설정액(억원)", "설정원본"],
    # 스냅샷형 공시 수익률(%) 컬럼
    "ret_1D": ["1일", "일간수익률", "전일대비"],
    "ret_1W": ["1주", "1주수익률"],
    "ret_1M": ["1개월", "1개월수익률"],
    "ret_3M": ["3개월", "3개월수익률"],
    "ret_6M": ["6개월", "6개월수익률"],
    "ret_YTD": ["연초이후", "YTD", "연초대비"],
    "ret_1Y": ["수익률(%)_1년", "1년", "1년수익률"],
    "ret_3Y": ["수익률(%)_3년", "3년", "3년수익률", "3년누적"],
}
# 금액 컬럼의 엑셀 단위 → 억 원 환산 계수 (확인 필요: 원/백만원/억원 중 무엇인지)
AMOUNT_TO_100M = {"원": 1e-8, "천원": 1e-5, "백만원": 1e-2, "억원": 1.0}
AMOUNT_UNIT = "억원"

# 보험사명 → insurer_code (확인 필요: 사내 약칭 기준으로 채울 것)
INSURER_CODES: dict[str, str] = {
    # "○○생명": "XXXX",
}

# 공시 원문 유형 → enum (위에서부터 첫 매칭; 순서 중요)
FUND_TYPE_RULES = [
    ("MMF", "MMF"), ("머니마켓", "MMF"),
    ("주식혼합", "주식혼합형"), ("채권혼합", "채권혼합형"),
    ("주식", "주식형"), ("채권", "채권형"),
    # 공시실 소유형 중 스키마 enum 밖의 값 → '기타' (원문 구분 유지 여부는 스키마 제안 대상)
    ("기타", "기타"), ("부동산", "기타"), ("커머더티", "기타"), ("원자재", "기타"),
]
REGION_RULES = [("글로벌", "글로벌혼합"), ("혼합", "글로벌혼합"), ("해외", "해외"), ("국내", "국내")]

PERIODS = ["1D", "1W", "1M", "3M", "6M", "YTD", "1Y", "3Y"]
RANK_PERIODS = ["1M", "1Y"]          # data_schema.md rank_in_type 정의
SOURCE = "생명보험협회 공시실"

# ───────────────────────── 2. 컬럼 매핑 ─────────────────────────
def norm(s):
    s = re.sub(r"(내림차순|오름차순)\s*정렬하기.*$", "", str(s))   # 공시실 헤더의 정렬 버튼 문구 제거
    return s.replace(" ", "").replace("\n", "").strip()

def map_columns(df):
    by_norm = {norm(c): c for c in df.columns}
    mapping, used = {}, set()
    for field, aliases in COLUMN_ALIASES.items():
        for a in aliases:
            if norm(a) in by_norm and by_norm[norm(a)] not in used:
                mapping[field] = by_norm[norm(a)]; used.add(mapping[field]); break
    unmapped_src = [c for c in df.columns if c not in used]
    missing_std = [f for f in COLUMN_ALIASES if f not in mapping]
    return mapping, unmapped_src, missing_std

def report(mapping, unmapped_src, missing_std):
    print("== 매핑됨 (표준필드 ← 엑셀컬럼) ==")
    for k, v in mapping.items(): print(f"  {k:<16} ← {v}")
    print(f"\n== 매핑 안 된 엑셀 컬럼 ({len(unmapped_src)}개) ==")
    for c in unmapped_src: print(f"  - {c}")
    print(f"\n== 엑셀에서 못 찾은 표준필드 ({len(missing_std)}개) ==")
    for f in missing_std: print(f"  - {f}")
    print()

# ───────────────────────── 3. 정규화 ─────────────────────────
def classify(raw, rules, default):
    s = norm(raw) if pd.notna(raw) else ""
    for kw, val in rules:
        if kw in s: return val, True
    return default, False

def to_num(s):
    return pd.to_numeric(pd.Series(s).astype(str).str.replace(",", "").str.replace("%", "").str.strip(),
                         errors="coerce")

def parse_ret(v):
    """공시실 수익률 셀 → float(%). 예: '▲ 49.4249.42' → 49.42, '▼ 3.21-3.21' → -3.21, '-' → None
    (화면표시값+정렬용 원값이 붙어 읽힘 → 마지막 소수 2자리 숫자를 원값으로 사용, 화살표로 부호 보정)"""
    if v is None or (isinstance(v, float) and pd.isna(v)): return None
    s = str(v).replace(",", "").strip()
    nums = re.findall(r"-?\d+\.\d{2}", s)
    if not nums:
        try: return float(s)
        except ValueError: return None
    x = float(nums[-1])
    if "▼" in s and x > 0: x = -x
    if "▲" in s and x < 0: x = -x
    return x

def r2(x):
    return None if x is None or pd.isna(x) else round(float(x), 2)

# ───────────────────────── 4. 수익률 계산 (이력형) ─────────────────────────
def period_start(base: pd.Timestamp, p: str) -> pd.Timestamp | None:
    off = {"1W": pd.DateOffset(weeks=1), "1M": pd.DateOffset(months=1), "3M": pd.DateOffset(months=3),
           "6M": pd.DateOffset(months=6), "1Y": pd.DateOffset(years=1), "3Y": pd.DateOffset(years=3)}
    if p == "YTD": return pd.Timestamp(base.year - 1, 12, 31)   # 전년 말 기준가 대비
    return base - off[p] if p in off else None

def returns_from_history(h: pd.DataFrame, base: pd.Timestamp, inception):
    """h: 한 펀드의 [base_date, nav] 오름차순. 시작일이 휴장일이면 직전 영업일 기준가."""
    h = h[h.base_date <= base]
    if h.empty or h.base_date.iloc[-1] != base: return {p: None for p in PERIODS}
    nav_t, out = h.nav.iloc[-1], {}
    for p in PERIODS:
        if p == "1D":
            prev = h.iloc[:-1]
            out[p] = r2((nav_t / prev.nav.iloc[-1] - 1) * 100) if len(prev) else None
            continue
        start = period_start(base, p)
        # 규칙 4: 설정일이 기간 시작일보다 늦으면 계산하지 않음
        if inception is not None and pd.notna(inception) and pd.Timestamp(inception) > start:
            out[p] = None; continue
        prior = h[h.base_date <= start]
        out[p] = r2((nav_t / prior.nav.iloc[-1] - 1) * 100) if len(prior) else None
    return out

def spark_from_history(h, base, n=52):
    h = h[(h.base_date <= base) & (h.base_date > base - pd.DateOffset(weeks=n))].set_index("base_date").nav
    if h.empty: return []
    weekly = h.resample("W-FRI").last().dropna()   # 주말 기준(금요일, 직전 영업일 값)
    return [round(float(v), 2) for v in weekly.tail(n)]

# ───────────────────────── 5. 메인 ─────────────────────────
def build(df, m, warnings):
    req = ["insurer_name", "fund_name", "nav", "net_assets_100m"]
    lack = [f for f in req if f not in m]
    if lack: sys.exit(f"[중단] 필수 표준필드 매핑 실패: {lack} → COLUMN_ALIASES 보완 필요")

    d = pd.DataFrame({f: df[c] for f, c in m.items()})
    d["nav"] = to_num(d["nav"]).values
    d["net_assets_100m"] = to_num(d["net_assets_100m"]).values * AMOUNT_TO_100M[AMOUNT_UNIT]
    if "total_fee_pct" in d: d["total_fee_pct"] = to_num(d["total_fee_pct"]).values
    if "base_date" in d: d["base_date"] = pd.to_datetime(d["base_date"], errors="coerce")
    if "inception_date" in d: d["inception_date"] = pd.to_datetime(d["inception_date"], errors="coerce")
    if "fund_code" in d:
        d["fund_code"] = d["fund_code"].astype(str).str.strip()
    else:   # 공시실 표에 펀드코드 없음 → 보험사명|펀드명|설정일 해시로 대체 (확인 필요: 펀드명 변경 시 키가 바뀜)
        key = d.insurer_name.astype(str) + "|" + d.fund_name.astype(str).str.strip() + "|" + \
              (d["inception_date"].dt.strftime("%Y%m%d") if "inception_date" in d else "")
        d["fund_code"] = key.map(lambda k: "H" + hashlib.sha1(k.encode()).hexdigest()[:8])
        warnings.append("펀드코드 컬럼 없음 → 보험사명|펀드명|설정일 해시로 fund_code 생성")

    # insurer_code
    if "insurer_code" in d and d.insurer_code.notna().all() and (d.insurer_code.astype(str) != "").all():
        miss_ins = []                                   # 협회 회사코드 사용
    else:
        miss_ins = sorted(set(d.insurer_name) - set(INSURER_CODES))
        if miss_ins:
            warnings.append(f"INSURER_CODES 미등록 보험사 {len(miss_ins)}개 → 보험사명 그대로 코드로 사용: {miss_ins}")
        d["insurer_code"] = d.insurer_name.map(lambda n: INSURER_CODES.get(n, norm(n)))
    d["fund_id"] = d.insurer_code + "_" + d.fund_code

    # 키 안정성 점검: 같은 펀드(보험사명·펀드명·설정일)에 fund_id가 여러 개면 코드가 날짜별로 바뀐다는 뜻
    ident = d.insurer_name.astype(str) + "|" + d.fund_name.astype(str) + "|" + \
            (d["inception_date"].astype(str) if "inception_date" in d else "")
    multi = d.assign(_k=ident).groupby("_k").fund_id.nunique()
    if (multi > 1).any():
        warnings.append(f"fund_id 불안정: 펀드 {int((multi > 1).sum())}개가 날짜별로 다른 코드 → 키 규칙 재검토 필요")

    if "base_date" not in d or d.base_date.isna().all():
        sys.exit("[중단] 기준일 컬럼이 없거나 비어 있음 → 기준일 없이 latest.json 생성 불가")
    base = d.base_date.max()
    is_history = d.groupby("fund_id").base_date.nunique().max() > 1
    print(f"기준일 {base.date()} · 입력형태 {'이력형(수익률 직접 계산)' if is_history else '스냅샷형(공시 수익률 사용)'}")

    snap = d.sort_values("base_date").groupby("fund_id").tail(1)
    snap = snap[snap.base_date == base]
    dropped = d.fund_id.nunique() - len(snap)
    if dropped: warnings.append(f"기준일({base.date()}) 기준가 없는 펀드 {dropped}개 제외")

    unk_type, unk_region, funds = set(), set(), []
    for _, r in snap.iterrows():
        ft, ok = classify(r.get("fund_type_raw"), FUND_TYPE_RULES, "기타")
        if not ok: unk_type.add(str(r.get("fund_type_raw")))
        rg, ok2 = classify(r.get("region_raw"), REGION_RULES, None)
        if not ok2: unk_region.add(str(r.get("region_raw")))
        inc = r.get("inception_date")

        if is_history:
            h = d[d.fund_id == r.fund_id][["base_date", "nav"]].dropna().sort_values("base_date")
            ret, spark = returns_from_history(h, base, inc), spark_from_history(h, base)
        else:
            ret, spark = {p: None for p in PERIODS}, []
        # 공시 수익률 컬럼이 있으면 우선 사용(1Y·3Y 등 이력 없이도 확보). 규칙 4: 설정일 미달이면 N/A
        for p in PERIODS:
            if f"ret_{p}" not in m: continue
            v = parse_ret(r.get(f"ret_{p}"))
            start = period_start(base, p)
            if start is not None and inc is not None and pd.notna(inc) and pd.Timestamp(inc) > start: v = None
            if v is not None or not is_history: ret[p] = r2(v)

        funds.append({
            "fund_id": r.fund_id, "insurer_name": r.insurer_name, "fund_name": str(r.fund_name).strip(),
            "fund_type": ft, "region": rg, "manager": None if pd.isna(r.get("manager")) else r.get("manager"),
            "nav": r2(r.nav), "net_assets_100m": r2(r.net_assets_100m),
            "total_fee_pct": r2(r.get("total_fee_pct")),
            "ret": ret, "rank_in_type": {}, "spark_1y": spark,
        })

    if unk_type: warnings.append(f"fund_type 미분류 → '기타' 처리: {sorted(unk_type)}")
    if unk_region: warnings.append(f"region 미분류 → null: {sorted(unk_region)}")
    if not is_history: warnings.append("이력 1일치: 1D~YTD·spark_1y 는 기준가 이력 누적(백필) 후 계산됨")

    # rank_in_type: 같은 fund_type 내 백분위(0~1, 1=최상위, 동률 평균), n<2 → 0.5
    fdf = pd.DataFrame([{"i": i, "t": f["fund_type"], **{p: f["ret"][p] for p in RANK_PERIODS}}
                        for i, f in enumerate(funds)])
    for p in RANK_PERIODS:
        for _, g in fdf.dropna(subset=[p]).groupby("t"):
            n = len(g)
            pct = (g[p].rank(method="average") - 1) / (n - 1) if n > 1 else pd.Series(0.5, index=g.index)
            for idx, v in pct.items(): funds[int(fdf.loc[idx, "i"])]["rank_in_type"][p] = round(float(v), 4)
        for f in funds: f["rank_in_type"].setdefault(p, None)

    return {"base_date": str(base.date()), "source": SOURCE, "funds": funds}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx"); ap.add_argument("-o", "--out", default="data/latest.json")
    ap.add_argument("--sheet", default=0); ap.add_argument("--header-row", type=int, default=0)
    ap.add_argument("--inspect", action="store_true")
    a = ap.parse_args()

    if os.path.isdir(a.xlsx):               # scrape_klia.py 월별 분할 이력(data/raw)
        files = sorted(glob.glob(os.path.join(a.xlsx, "nav_*.csv*")))
        if not files: sys.exit(f"[중단] {a.xlsx} 에 nav_*.csv(.gz) 없음 → 먼저 scrape_klia.py 실행")
        df = pd.concat([pd.read_csv(f, dtype=str, encoding="utf-8-sig", keep_default_na=False).replace("", pd.NA)
                        for f in files], ignore_index=True)
        print(f"이력 파일 {len(files)}개 · {len(df):,}행")
    elif a.xlsx.lower().endswith((".csv", ".csv.gz")):
        df = pd.read_csv(a.xlsx, header=a.header_row, dtype=str, encoding="utf-8-sig")
    else:
        df = pd.read_excel(a.xlsx, sheet_name=a.sheet, header=a.header_row, dtype=str)
    df = df.dropna(how="all").dropna(axis=1, how="all")
    df.columns = [str(c).strip() for c in df.columns]
    m, unmapped, missing = map_columns(df)
    report(m, unmapped, missing)
    if a.inspect: return

    warnings = []
    out = build(df, m, warnings)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f: json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"→ {a.out} 저장 · 펀드 {len(out['funds'])}개")
    for w in warnings: print("  [경고]", w)

if __name__ == "__main__":
    main()
