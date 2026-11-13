#!/usr/bin/env python3
"""
주요 지수 종가 수집 → data/market.json (화면 '오늘의 요약' 아래 지수 칸)

  DATA_GO_KR_KEY=발급키 python scripts/fetch_market.py

출처별 상태
- 코스피: 금융위원회_지수시세정보 (공공데이터포털 data.go.kr, 무료·활용신청 후 인증키)  ← 사용
- S&P500·다우존스: S&P Dow Jones Indices 사전 서면 허가 없이 재게시 불가 → 미사용(허가·라이선스 확보 시 추가)
- 나스닥 종합: Nasdaq 저작권 고지 → 미사용(동일)

인증키가 없으면 아무것도 바꾸지 않고 종료(기존 market.json 유지).
API 경로·필드명은 공공데이터포털 활용가이드 기준(확인 필요: 첫 실행 로그로 검증).
"""
import json, os, sys
from datetime import date, datetime, timedelta, timezone
import requests

OUT = "data/market.json"
KEY = os.environ.get("DATA_GO_KR_KEY", "").strip()
URL = "https://apis.data.go.kr/1160100/service/GetMarketIndexInfoService/getStockMarketIndex"
KST = timezone(timedelta(hours=9))


def kospi():
    begin = (datetime.now(KST).date() - timedelta(days=14)).strftime("%Y%m%d")
    r = requests.get(URL, params={"serviceKey": KEY, "resultType": "json", "numOfRows": 30, "pageNo": 1,
                                  "idxNm": "코스피", "beginBasDt": begin}, timeout=30)
    r.raise_for_status()
    try:
        items = r.json()["response"]["body"]["items"]["item"]
    except Exception:
        sys.exit(f"응답 형식 확인 필요: {r.text[:300].replace(KEY, '***')}")
    items = [i for i in (items if isinstance(items, list) else [items]) if i.get("idxNm") == "코스피"]
    if not items:
        sys.exit("[중단] 코스피 항목 없음")
    last = max(items, key=lambda i: i["basDt"])
    d = last["basDt"]
    return {"name": "코스피", "close": round(float(last["clpr"]), 2), "chg_pct": round(float(last["fltRt"]), 2),
            "date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "source": "금융위원회 지수시세정보(공공데이터포털)"}


def status(msg):
    """진단용 상태 기록(인증키는 기록하지 않음)"""
    os.makedirs("data", exist_ok=True)
    with open("data/market_status.json", "w", encoding="utf-8") as f:
        json.dump({"result": msg}, f, ensure_ascii=False, indent=1)
    print(msg)


def main():
    if not KEY:
        status("DATA_GO_KR_KEY 없음 → 지수 수집 건너뜀")
        return
    try:
        k = kospi()
    except SystemExit as e:
        status(f"실패: {e}"); return
    except Exception as e:
        status(f"실패: {type(e).__name__}: {str(e).replace(KEY, '***')[:300]}"); return
    out = {"updated": datetime.now(KST).isoformat(timespec="minutes"),
           "source_note": "코스피 — 금융위원회 지수시세정보(공공데이터포털)",
           "indices": [k]}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    status(f"성공: 코스피 {k['date']} {k['close']}")


if __name__ == "__main__":
    main()
