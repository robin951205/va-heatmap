#!/usr/bin/env python3
"""
생명보험협회 공시실 > 상품비교공시 > 변액보험 > 펀드현황(일별) 수집기

  python scrape_klia.py --anchors                # [일 배치 기본] 기준일 + 각 기간 시작일(1D·1W·1M·3M·6M·YTD) 기준가만 조회
  python scrape_klia.py --probe                  # [먼저 실행] 1페이지만 받아 표 구조 진단 + HTML 저장
  python scrape_klia.py                          # 직전 영업일 1일치 수집(전 페이지)
  python scrape_klia.py --date 2026-11-11        # 특정 기준일
  python scrape_klia.py --backfill 2025-11-01 2026-11-11   # 과거 이력 적재

결과: data/raw/nav_YYYY-MM.csv.gz 에 월별로 누적(중복 제거) → build_latest.py data/raw 로 latest.json 생성.
      (월별 분할·압축: GitHub 파일 100MB 제한 회피, 매일 커밋 크기 최소화)
※ 협회 이용약관·자동수집 허용 여부 확인 전에는 개인 확인용으로만 사용(확인 필요).
"""
import argparse, contextlib, glob, io, json, os, re, signal, sys, time
from datetime import date, timedelta
import pandas as pd
import requests

CONFIG = os.environ.get("KLIA_CONFIG", "klia_request.json")
RAW_DIR = os.environ.get("KLIA_RAW_DIR", "data/raw")   # 월별 분할 저장: nav_YYYY-MM.csv.gz
SLEEP_SEC = 2.0          # 요청 간격(서버 부담 최소화)
MAX_RETRY = 3
REQUEST_DEADLINE = 150   # 요청 1건 전체 제한(초): 응답이 느리게 흘러들어와 무한 대기하는 것 방지(2026-11-12 백필 정지 사례)

@contextlib.contextmanager
def deadline(sec):
    """SIGALRM 으로 블로킹 소켓 읽기까지 강제 중단 (Linux/macOS, 메인 스레드)"""
    if not hasattr(signal, "SIGALRM"):
        yield; return
    def _raise(*_): raise TimeoutError(f"{sec}초 초과")
    old = signal.signal(signal.SIGALRM, _raise); signal.alarm(sec)
    try: yield
    finally:
        signal.alarm(0); signal.signal(signal.SIGALRM, old)

class DayFailed(Exception):
    """한 기준일 수집 실패 — 백필에서는 건너뛰고 다음 날로 진행"""

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
                p = re.sub(r"\s*(내림차순|오름차순)\s*정렬하기.*$", "", p).strip()   # 정렬 버튼 문구 제거
                if p and not p.startswith("Unnamed") and p not in parts: parts.append(p)
            cols.append("_".join(parts))
        df.columns = cols
    df.columns = [re.sub(r"\s*(내림차순|오름차순)\s*정렬하기.*$", "", str(c)).strip() for c in df.columns]
    return df

def html_tables(text):
    try:
        return [flatten_cols(t) for t in pd.read_html(io.StringIO(text))]
    except ValueError:            # 표 없음
        return []

def checkbox_values(text):
    """목록 표의 각 행 체크박스 value(문서 순서). 없으면 []."""
    try:
        from lxml import html as lh
        doc = lh.fromstring(text)
    except Exception:
        return []
    out = []
    for tr in doc.xpath("//tr[.//input[@type='checkbox']]"):
        if tr.xpath("ancestor::thead"): continue          # 헤더의 '전체선택' 제외
        inp = tr.xpath(".//input[@type='checkbox']")[0]
        out.append((inp.get("value") or "").strip())
    return out

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
        codes = checkbox_values(resp.text)
        if len(codes) == len(df) and any(codes):
            # 체크박스 value 예: '20261111L01KLVL01V5205' = 기준일(8) + 협회 회사코드(L01) + 펀드코드
            df = df.copy()
            rest = [re.sub(r"^\d{8}", "", v) for v in codes]
            df["회사코드"] = [m.group(1) if (m := re.match(r"^(L\d{2})", v)) else "" for v in rest]
            df["펀드코드"] = [re.sub(r"^L\d{2}", "", v) for v in rest]
    elif kind == "json":
        data = resp.json()
        for key in c.get("json_path", []): data = data[key]
        df = pd.DataFrame(data)
    elif kind == "excel":
        df = pd.read_excel(io.BytesIO(resp.content), header=c.get("header_row", 0), dtype=str)
    else:
        raise ValueError(kind)
    df = flatten_cols(df)
    if is_empty_result(df): return pd.DataFrame()
    need = [k for k in c.get("required_columns", []) if k not in df.columns]
    if need:
        sys.exit(f"[중단] 응답 표에 필수 컬럼 없음 {need} (인코딩/화면구조 변경 의심). 받은 컬럼: {list(df.columns)[:8]}")
    return df

# ───────────────────────── 요청 ─────────────────────────
def request(sess, c, d: date, page: int):
    fmt = c.get("date_format", "%Y-%m-%d")
    body = fill(c.get("params", {}), d, page, fmt)
    method = c.get("method", "GET").upper()
    kw = dict(method=method, url=c["url"], headers=c.get("headers", {}), timeout=(10, 60))
    if method == "GET": kw["params"] = body
    elif c.get("body") == "json": kw["json"] = body
    else: kw["data"] = body

    r = None
    for i in range(MAX_RETRY):
        t0 = time.monotonic()
        try:
            with deadline(REQUEST_DEADLINE):              # 요청 전체(연결~본문 수신)에 하드 제한
                r = sess.request(**kw)
        except (requests.RequestException, TimeoutError) as e:
            print(f"  요청 오류 재시도 {i+1}/{MAX_RETRY}: {e}", flush=True)
            r = None; sess.close(); time.sleep(SLEEP_SEC * (i + 3)); continue
        print(f"  응답 {time.monotonic() - t0:.1f}초 · {len(r.content)/1e6:.1f}MB", flush=True)
        if r.status_code in (403, 429):
            sys.exit(f"[중단] HTTP {r.status_code} — 접근 제한. 반복 호출하지 말 것")
        if r.ok:
            # 인코딩 자동추정(apparent_encoding)은 페이지마다 틀릴 수 있음(2026-11-12 일부 페이지가 키릴 문자로 깨짐)
            # → 응답 헤더 charset > HTML meta charset > utf-8 순으로 고정
            if "charset=" not in r.headers.get("Content-Type", "").lower():
                m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", r.content[:5000], re.I)
                r.encoding = m.group(1).decode() if m else "utf-8"
            return r
        time.sleep(SLEEP_SEC * (i + 3))
    raise DayFailed(f"{d} 요청 {MAX_RETRY}회 실패" + (f" (HTTP {r.status_code})" if r is not None else ""))

def fetch_day(sess, c, d: date) -> pd.DataFrame:
    pages, prev_sig, first_n = [], None, None
    max_pages = c.get("max_pages", 300) if c.get("paginate") else 1
    for page in range(1, max_pages + 1):
        df = parse(request(sess, c, d, page), c)
        if df.empty: break
        sig = pd.util.hash_pandas_object(df, index=False).sum()
        if sig == prev_sig: break                 # 마지막 페이지를 반복 반환하는 경우
        pages.append(df); prev_sig = sig
        first_n = first_n or len(df)              # 서버가 실제 적용한 페이지 크기(요청값보다 작을 수 있음)
        print(f"    p{page}: {len(df)}행")
        if len(df) < first_n: break               # 마지막 페이지
        time.sleep(SLEEP_SEC)
    if not pages: return pd.DataFrame()
    out = pd.concat(pages, ignore_index=True)
    keys = [k for k in ("회사코드", "펀드코드") if k in out.columns]
    if len(keys) == 2:
        dup = int(out.duplicated(subset=keys).sum())
        print(f"    수신 {len(out)}행 · 고유 펀드 {len(out) - dup}개" +
              (f" · 페이지 간 중복 {dup}행 (페이지 경계 불안정 → 누락 가능성)" if dup else ""))
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
    codes = checkbox_values(r.text)
    print(f"\n행 체크박스 value {len(codes)}개 · 예시: {codes[:3]}")
    if not tables:
        print("→ 표가 없음: 결과가 자바스크립트로 따로 불려오는 구조일 수 있음. probe HTML 파일을 공유해 주세요.")

# ───────────────────────── 메인 ─────────────────────────
# ───────────────────────── 기준시점(앵커) 조회 ─────────────────────────
# 수익률 = 기준일 기준가 / 기간 시작일 기준가 − 1 → 시작일 기준가만 있으면 됨.
# 1Y·3Y·5Y는 공시실이 수익률을 직접 제공하므로 조회하지 않음.
ANCHOR_BACK_DAYS = 10       # 시작일이 휴장이면 직전 영업일로 최대 10일까지 거슬러 올라감

def today_kst() -> date:
    from zoneinfo import ZoneInfo
    from datetime import datetime
    return datetime.now(ZoneInfo("Asia/Seoul")).date()

def anchor_dates(base: date) -> dict:
    ts = pd.Timestamp(base)
    return {
        "1D":  base - timedelta(days=1),
        "1W":  base - timedelta(days=7),
        "1M":  (ts - pd.DateOffset(months=1)).date(),
        "3M":  (ts - pd.DateOffset(months=3)).date(),
        "6M":  (ts - pd.DateOffset(months=6)).date(),
        "YTD": date(base.year - 1, 12, 31),
    }

def find_available(sess, c, d: date, have: set, label: str, min_rows: int = 0):
    """d 또는 그 직전 영업일 중 공시 기준가가 있는 날을 찾아 저장. 반환: 실제 사용 일자(없으면 None)
    min_rows: 이보다 적게 공시된 날은 '일부 보험사 미반영(공시 진행 중)'으로 보고 저장하지 않고 직전 영업일 사용"""
    for k in range(ANCHOR_BACK_DAYS + 1):
        day = d - timedelta(days=k)
        if day.weekday() >= 5: continue
        if day.isoformat() in have:
            print(f"  {label:<4} {day} (저장분 사용)"); return day
        print(f"  {label:<4} {day} 조회", flush=True)
        try:
            df = fetch_day(sess, c, day)
        except DayFailed as e:
            print(f"    [실패] {e}"); return None
        time.sleep(SLEEP_SEC)
        if df.empty: print("    → 공시 없음(휴장/미확정), 직전 영업일 시도"); continue
        if min_rows and len(df) < min_rows:
            print(f"    → {len(df)}행 < 기준 {min_rows}행: 공시 진행 중으로 보고 저장 안 함, 직전 영업일 사용"); continue
        path, n = append(df, c, day); have.add(day.isoformat())
        print(f"    → 저장 {path} ({len(df)}행)"); return day
    print(f"  [경고] {label}: {d} 이전 {ANCHOR_BACK_DAYS}일 내 공시 없음"); return None

def run_anchors(sess, c, target: date):
    have = existing_dates(c)
    print(f"[기준시점 조회] 목표 기준일 {target}")
    # 직전 저장 기준일 행수의 95% 미만이면 공시 진행 중으로 판단 (다음 보완 실행에서 다시 시도)
    min_rows = 0
    if have:
        last = max(have); f = os.path.join(RAW_DIR, f"nav_{last[:7]}.csv.gz")
        try: min_rows = int((pd.read_csv(f, dtype=str, usecols=[c.get("date_column", "기준일")])[c.get("date_column", "기준일")] == last).sum() * 0.95)
        except Exception: min_rows = 0
    base = find_available(sess, c, target, have, "기준일", min_rows)
    if base is None: sys.exit("[중단] 기준일 기준가를 찾지 못함")
    used = {"기준일": base}
    for p, d in anchor_dates(base).items():
        used[p] = find_available(sess, c, d, have, p)
    print("사용 일자: " + ", ".join(f"{k}={v}" for k, v in used.items()))
    miss = [k for k, v in used.items() if v is None]
    if miss: print(f"[경고] 시작일 기준가 없음 → 해당 기간 N/A: {miss}")

def prune(months: int):
    cutoff = (pd.Timestamp(today_kst()) - pd.DateOffset(months=months)).strftime("%Y-%m")
    for f in sorted(glob.glob(os.path.join(RAW_DIR, "nav_*.csv.gz"))):
        ym = os.path.basename(f)[4:11]
        if ym < cutoff:
            os.remove(f); print(f"  정리: {f} ({months}개월 초과)")

def prev_business_day(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5: d -= timedelta(days=1)
    return d

def business_days(a: date, b: date):
    d = a
    while d <= b:
        if d.weekday() < 5: yield d
        d += timedelta(days=1)

def existing_dates(c):
    col = c.get("date_column", "기준일"); out = set()
    for f in glob.glob(os.path.join(RAW_DIR, "nav_*.csv.gz")):
        try: out |= set(pd.read_csv(f, dtype=str, usecols=[col], keep_default_na=False)[col])
        except Exception as e: print(f"  [경고] {f} 읽기 실패: {e}")
    return out

def dedupe(rows, c):
    keys = c.get("dedupe_keys", [])
    if keys and all(k in rows.columns for k in keys):
        ok = rows[keys].replace("", pd.NA).notna().all(axis=1)
        if (~ok).any(): print(f"  [경고] 중복제거 키가 빈 행 {int((~ok).sum())}개 → 그대로 보존")
        return pd.concat([rows[ok].drop_duplicates(subset=keys, keep="last"), rows[~ok]], ignore_index=True)
    if keys: print(f"  [경고] 중복제거 키 {keys} 중 일부 컬럼 없음 → 행 전체 기준으로 중복 제거")
    return rows.drop_duplicates(keep="last")

def append(rows: pd.DataFrame, c, d: date):
    """기준일 d 의 수집분을 해당 월 파일에 병합 저장. 반환: 그 월 파일의 총 행수."""
    os.makedirs(RAW_DIR, exist_ok=True)
    path = os.path.join(RAW_DIR, f"nav_{d:%Y-%m}.csv.gz")
    rows = rows.apply(lambda col: col.map(lambda v: "" if pd.isna(v) else str(v)))   # NaN → 빈칸(경고 없이)
    if os.path.exists(path):
        rows = pd.concat([pd.read_csv(path, dtype=str, keep_default_na=False), rows], ignore_index=True)
    rows = dedupe(rows, c)
    rows.to_csv(path, index=False, encoding="utf-8", compression="gzip")
    return path, len(rows)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date"); ap.add_argument("--backfill", nargs=2, metavar=("START", "END"))
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--skip-existing", action="store_true", help="이미 저장된 기준일은 건너뜀(백필 재개용)")
    ap.add_argument("--anchors", action="store_true", help="기준일과 기간 시작일 기준가만 조회")
    ap.add_argument("--prune-months", type=int, default=0, help="N개월보다 오래된 원본 월 파일 삭제(0=안 함)")
    a = ap.parse_args()
    c = load_config()
    sess = requests.Session()
    sess.headers["User-Agent"] = c.get("user_agent", "Mozilla/5.0")

    # 기준일 = 공시실 조회일자. D일 기준가 = D-1일까지 시장 성과 반영, D일 아침 공시 → 당일부터 조회
    target = date.fromisoformat(a.date) if a.date else today_kst()
    if a.probe: return probe(sess, c, target)
    if a.anchors:
        run_anchors(sess, c, target)
        if a.prune_months: prune(a.prune_months)
        return

    days = (list(business_days(date.fromisoformat(a.backfill[0]), date.fromisoformat(a.backfill[1])))
            if a.backfill else [target])
    if a.skip_existing:
        have = existing_dates(c)
        before = len(days); days = [d for d in days if d.isoformat() not in have]
        print(f"이미 저장된 {before - len(days)}일 건너뜀 → 대상 {len(days)}일")
    saved, empty, failed = 0, [], []
    for i, d in enumerate(days):
        print(f"[{i+1}/{len(days)}] 기준일 {d}", flush=True)
        try:
            df = fetch_day(sess, c, d)
        except DayFailed as e:
            print(f"  [건너뜀] {e}", flush=True); failed.append(d.isoformat())
            if len(failed) >= 5 and len(failed) > saved:     # 연속 실패가 많으면 서버 상태 문제 → 중단
                sys.exit(f"[중단] 실패 {len(failed)}일 (저장 {saved}일): {failed}")
            continue
        if df.empty: empty.append(d.isoformat())
        else:
            path, n = append(df, c, d)            # 하루 단위 저장 → 백필 중단돼도 이어서 가능
            saved += 1; print(f"  → {path} ({n}행)")
        if i < len(days) - 1: time.sleep(SLEEP_SEC)

    print(f"완료 · 저장 {saved}일 · 빈 응답 {len(empty)}일 {empty[:5]} · 실패 {len(failed)}일 {failed}")
    if not saved and days: sys.exit("[중단] 수집 0건 (휴장일/기준가 미확정/조회조건 확인)")

if __name__ == "__main__":
    main()
