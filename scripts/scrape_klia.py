#!/usr/bin/env python3
"""
생명보험협회 공시실 > 상품비교공시 > 변액보험 > 펀드현황(일별) 수집기

  python scrape_klia.py --probe                  # [먼저 실행] 1페이지만 받아 표 구조 진단 + HTML 저장
  python scrape_klia.py                          # 직전 영업일 1일치 수집(전 페이지)
  python scrape_klia.py --date 2026-11-11        # 특정 기준일
  python scrape_klia.py --backfill 2025-11-01 2026-11-11   # 과거 이력 적재

결과: data/raw/nav_YYYY-MM.csv.gz 에 월별로 누적(중복 제거) → build_latest.py data/raw 로 latest.json 생성.
      (월별 분할·압축: GitHub 파일 100MB 제한 회피, 매일 커밋 크기 최소화)
※ 협회 이용약관·자동수집 허용 여부 확인 전에는 개인 확인용으로만 사용(확인 필요).
"""
import argparse, io, json, os, sys, time
from datetime import date, timedelta
import pandas as pd
import requests

CONFIG = os.environ.get("KLIA_CONFIG", "klia_request.json")
RAW_DIR = os.environ.get("KLIA_RAW_DIR", "data/raw")   # 월별 분할 저장: nav_YYYY-MM.csv.gz
SLEEP_SEC = 2.0          # 요청 간격(서버 부담 최소화)
MAX_RETRY = 3

# ───────────────────────── 설정 ─────────────────────────
def load_config():
    with open(CONFIG, encoding="utf-8") as f:
        c = json.load(f)
    if "TODO" in json.dumps(c, ensure_ascii=False):
        sys.exit(f"[중단] {CONFIG} 에 TODO 가 남아 있음")
    return c

def fill(obj, d: date, page: int, fmt: str):
    """'{date}', '{date_1y}', '{page}' 자리표시자 치환(dict/list/str 재귀)."""
    if isinstance(obj, dict): return {k: fill(v, d, page, fmt) for k, v in obj.items()}
    if isinstance(obj, list): return [fill(v, d, page, fmt) for v in obj]
    if isinstance(obj, str):
        try: d1y = d.replace(year=d.year - 1)
        except ValueError: d1y = d.replace(year=d.year - 1, day=28)   # 2/29
        return (obj.replace("{date_1y}", d1y.strftime(fmt))
                   .replace("{date}", d.strftime(fmt))
                   .replace("{page}", str(page)))
    return obj

# ───────────────────────── 응답 파싱 ─────────────────────────
def flatten_cols(df):
    if isinstance(df.columns, pd.MultiIndex):
        cols = []
        for tup in df.columns:
            parts = []
            for p in map(str, tup):
                p = p.strip()
                if p and not p.startswith("Unnamed") and p not in parts: parts.append(p)
            cols.append("_".join(parts))
        df.columns = cols
    df.columns = [str(c).strip() for c in df.columns]
    return df

def html_tables(text):
    try:
        return [flatten_cols(t) for t in pd.read_html(io.StringIO(text))]
    except ValueError:            # 표 없음
        return []

def pick_table(tables, c):
    if not tables: return pd.DataFrame()
    idx = c.get("table_index")
    if idx is not None: return tables[idx]
    return max(tables, key=len)   # 기본: 행이 가장 많은 표 = 펀드 목록

def is_empty_result(df):
    """'조회된 데이터가 없습니다' 같은 1행 안내문을 빈 결과로 판단."""
    if df.empty: return True
    if len(df) == 1 and df.iloc[0].astype(str).nunique() == 1: return True
    return False

def parse(resp, c) -> pd.DataFrame:
    kind = c.get("response", "html")
    if kind == "html":
        df = pick_table(html_tables(resp.text), c)
    elif kind == "json":
        data = resp.json()
        for key in c.get("json_path", []): data = data[key]
        df = pd.DataFrame(data)
    elif kind == "excel":
        df = pd.read_excel(io.BytesIO(resp.content), header=c.get("header_row", 0), dtype=str)
    else:
        raise ValueError(kind)
    df = flatten_cols(df)
    return pd.DataFrame() if is_empty_result(df) else df

# ───────────────────────── 요청 ─────────────────────────
def request(sess, c, d: date, page: int):
    fmt = c.get("date_format", "%Y-%m-%d")
    body = fill(c.get("params", {}), d, page, fmt)
    method = c.get("method", "GET").upper()
    kw = dict(method=method, url=c["url"], headers=c.get("headers", {}), timeout=30)
    if method == "GET": kw["params"] = body
    elif c.get("body") == "json": kw["json"] = body
    else: kw["data"] = body

    for i in range(MAX_RETRY):
        try:
            r = sess.request(**kw)
        except requests.RequestException as e:
            print(f"  네트워크 오류 재시도 {i+1}: {e}"); time.sleep(SLEEP_SEC * (i + 2)); continue
        if r.status_code in (403, 429):
            sys.exit(f"[중단] HTTP {r.status_code} — 접근 제한. 반복 호출하지 말 것")
        if r.ok:
            r.encoding = r.apparent_encoding or "utf-8"
            return r
        time.sleep(SLEEP_SEC * (i + 2))
    sys.exit(f"[중단] 요청 실패 HTTP {r.status_code}: {r.url[:200]}")

def fetch_day(sess, c, d: date) -> pd.DataFrame:
    pages, prev_sig = [], None
    max_pages = c.get("max_pages", 300) if c.get("paginate") else 1
    unit = int(c.get("params", {}).get("pageUnit", 0) or 0)
    for page in range(1, max_pages + 1):
        df = parse(request(sess, c, d, page), c)
        if df.empty: break
        sig = pd.util.hash_pandas_object(df, index=False).sum()
        if sig == prev_sig: break                 # 마지막 페이지를 반복 반환하는 경우
        pages.append(df); prev_sig = sig
        print(f"    p{page}: {len(df)}행")
        if unit and len(df) < unit: break         # 마지막 페이지
        time.sleep(SLEEP_SEC)
    if not pages: return pd.DataFrame()
    out = pd.concat(pages, ignore_index=True)
    col = c.get("date_column", "기준일")
    if col not in out.columns: out[col] = d.isoformat()
    return out

# ───────────────────────── 진단 모드 ─────────────────────────
def probe(sess, c, d: date):
    r = request(sess, c, d, 1)
    os.makedirs("data/raw", exist_ok=True)
    path = f"data/raw/probe_{d:%Y%m%d}.html"
    with open(path, "w", encoding="utf-8") as f: f.write(r.text)
    print(f"요청 URL: {r.url[:300]}...")
    print(f"HTTP {r.status_code} · {len(r.text):,}자 · 원본 저장: {path}")
    tables = html_tables(r.text)
    print(f"\n표 {len(tables)}개 발견")
    for i, t in enumerate(tables):
        print(f"\n[표 {i}] {t.shape[0]}행 × {t.shape[1]}열")
        print("  컬럼:", list(t.columns))
        with pd.option_context("display.max_columns", 50, "display.width", 250):
            print(t.head(3).to_string())
    if not tables:
        print("→ 표가 없음: 결과가 자바스크립트로 따로 불려오는 구조일 수 있음. probe HTML 파일을 공유해 주세요.")

# ───────────────────────── 메인 ─────────────────────────
def prev_business_day(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5: d -= timedelta(days=1)
    return d

def business_days(a: date, b: date):
    d = a
    while d <= b:
        if d.weekday() < 5: yield d
        d += timedelta(days=1)

def dedupe(rows, c):
    keys = c.get("dedupe_keys", [])
    if keys and all(k in rows.columns for k in keys):
        return rows.drop_duplicates(subset=keys, keep="last")
    if keys: print(f"  [경고] 중복제거 키 {keys} 중 일부 컬럼 없음 → 행 전체 기준으로 중복 제거")
    return rows.drop_duplicates(keep="last")

def append(rows: pd.DataFrame, c, d: date):
    """기준일 d 의 수집분을 해당 월 파일에 병합 저장. 반환: 그 월 파일의 총 행수."""
    os.makedirs(RAW_DIR, exist_ok=True)
    path = os.path.join(RAW_DIR, f"nav_{d:%Y-%m}.csv.gz")
    rows = rows.astype(str)
    if os.path.exists(path):
        rows = pd.concat([pd.read_csv(path, dtype=str, keep_default_na=False), rows], ignore_index=True)
    rows = dedupe(rows, c)
    rows.to_csv(path, index=False, encoding="utf-8", compression="gzip")
    return path, len(rows)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date"); ap.add_argument("--backfill", nargs=2, metavar=("START", "END"))
    ap.add_argument("--probe", action="store_true")
    a = ap.parse_args()
    c = load_config()
    sess = requests.Session()
    sess.headers["User-Agent"] = c.get("user_agent", "Mozilla/5.0")

    target = date.fromisoformat(a.date) if a.date else prev_business_day(date.today())
    if a.probe: return probe(sess, c, target)

    days = (list(business_days(date.fromisoformat(a.backfill[0]), date.fromisoformat(a.backfill[1])))
            if a.backfill else [target])
    saved, empty = 0, []
    for i, d in enumerate(days):
        print(f"[{i+1}/{len(days)}] 기준일 {d}")
        df = fetch_day(sess, c, d)
        if df.empty: empty.append(d.isoformat())
        else:
            path, n = append(df, c, d)            # 하루 단위 저장 → 백필 중단돼도 이어서 가능
            saved += 1; print(f"  → {path} ({n}행)")
        if i < len(days) - 1: time.sleep(SLEEP_SEC)

    if not saved: sys.exit(f"[중단] 수집 0건 (휴장일/기준가 미확정/조회조건 확인): {empty}")
    print(f"완료 · 저장 {saved}일 · 빈 응답 {len(empty)}일 {empty[:5]}")

if __name__ == "__main__":
    main()
