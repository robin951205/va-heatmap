#!/usr/bin/env python3
"""
시장 지표 수집 → data/indicators.json  (화면 '시장 지표' 칸)

  ECOS_API_KEY=발급키 python scripts/fetch_indicators.py

- 국고채 3년·10년 금리: 한국은행 ECOS 817Y002(시장금리, 일별) 010200000 / 010210000
- 원/달러 환율(매매기준율): 한국은행 ECOS 731Y001(주요국 통화의 대원화환율, 일별) 0000001
- VIX: 숫자는 화면에서 TradingView 위젯으로 표시. Cboe 저작권 데이터(재배포 허가 필요)라
  여기서는 FRED(VIXCLS) 전일 종가로 '구간'만 계산해 저장하고 숫자는 저장·게시하지 않음

키가 없거나 실패하면 기존 indicators.json 은 그대로 두고 data/indicators_status.json 에 사유만 기록.
"""
import json, os
from datetime import datetime, timedelta, timezone

import requests

KST = timezone(timedelta(hours=9))
KEY = os.environ.get("ECOS_API_KEY", "").strip()
OUT, STATUS = "data/indicators.json", "data/indicators_status.json"

SERIES = {
    "kr3y":   ("817Y002", "010200000", "국고채 3년"),
    "kr10y":  ("817Y002", "010210000", "국고채 10년"),
    "usdkrw": ("731Y001", "0000001",   "원/달러 환율(매매기준율)"),
}


def status(msg, ok=False):
    os.makedirs("data", exist_ok=True)
    with open(STATUS, "w", encoding="utf-8") as f:
        json.dump({"ok": ok, "result": msg}, f, ensure_ascii=False, indent=1)
    print(msg)


def ecos(stat, item, start, end):
    url = f"https://ecos.bok.or.kr/api/StatisticSearch/{KEY}/json/kr/1/1000/{stat}/D/{start}/{end}/{item}"
    r = requests.get(url, timeout=40)
    r.raise_for_status()
    d = r.json()
    if "StatisticSearch" not in d:
        raise RuntimeError(f"{stat}/{item}: {json.dumps(d.get('RESULT', d), ensure_ascii=False)[:200]}")
    rows = [(x["TIME"], float(x["DATA_VALUE"])) for x in d["StatisticSearch"]["row"] if x.get("DATA_VALUE") not in (None, "", "-")]
    rows.sort()
    if len(rows) < 2:
        raise RuntimeError(f"{stat}/{item}: 데이터 부족({len(rows)})")
    return rows


VIX_BANDS = [15, 20, 30]   # 매우 안정 / 안정 / 경계 / 불안 (화면 BANDS.vix 와 동일)


def vix_band():
    """FRED VIXCLS 최근 종가 → 구간 번호(0~3)와 날짜. 값은 반환하지 않음"""
    r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS", timeout=40,
                     headers={"user-agent": "va-heatmap-batch"})
    r.raise_for_status()
    rows = [ln.split(",") for ln in r.text.strip().splitlines()[1:]]
    rows = [(d, float(v)) for d, v in rows if v not in (".", "")]
    if not rows:
        raise RuntimeError("VIXCLS 데이터 없음")
    d, v = rows[-1]
    return {"date": d, "band": sum(v >= b for b in VIX_BANDS), "source": "Cboe(FRED VIXCLS) 전일 종가 기준 구간"}


def iso(t):
    return f"{t[:4]}-{t[4:6]}-{t[6:8]}"


def main():
    if not KEY:
        status("ECOS_API_KEY 없음 → 시장 지표 수집 건너뜀")
        return
    today = datetime.now(KST).date()
    start, end = (today - timedelta(days=380)).strftime("%Y%m%d"), today.strftime("%Y%m%d")
    out = {"updated": datetime.now(KST).isoformat(timespec="minutes"),
           "source": "한국은행 경제통계시스템(ECOS)", "items": {}}
    errs = []
    for k, (stat, item, name) in SERIES.items():
        try:
            rows = ecos(stat, item, start, end)
        except Exception as e:
            errs.append(f"{name}: {str(e).replace(KEY, '***')[:200]}")
            continue
        (t1, v1), (t0, v0) = rows[-1], rows[-2]
        vals = [v for _, v in rows]
        it = {"name": name, "date": iso(t1), "value": round(v1, 3), "prev": round(v0, 3), "prev_date": iso(t0),
              "min_1y": round(min(vals), 3), "max_1y": round(max(vals), 3), "n_1y": len(vals)}
        if k == "usdkrw":
            it["chg"] = round(v1 - v0, 2)
            below = sum(1 for v in vals if v < v1)
            it["pct_1y"] = round(100 * below / (len(vals) - 1), 1) if len(vals) > 1 else None   # 1년 중 현재보다 낮았던 날 비율
        else:
            it["chg_bp"] = round((v1 - v0) * 100, 1)   # %p → bp
        out["items"][k] = it
    try:
        out["vix_band"] = vix_band()
    except Exception as e:
        errs.append(f"VIX 구간: {str(e)[:200]}")
    if not out["items"]:
        status("실패: " + " / ".join(errs))
        return
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    status(("성공" if not errs else "일부 성공") + ": " + ", ".join(f"{v['name']} {v['date']} {v['value']}" for v in out["items"].values())
           + ((" / 실패: " + " / ".join(errs)) if errs else ""), ok=not errs)


if __name__ == "__main__":
    main()
