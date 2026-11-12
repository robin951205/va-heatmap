# VA Fund Heatmap — 변액보험펀드 기준가 히트맵

생명보험협회 공시실 '펀드현황(일별)' 기준가를 매일 자동 수집해 Finviz식 히트맵으로 보여주는 정적 웹사이트.
이용자는 주소만 열면 되고, 수집·계산은 GitHub Actions가 평일 1회 대신 실행한다.

```
index.html                     히트맵 화면 (data/latest.json 읽음, 없으면 MOCK 표시)
data/latest.json               배치가 생성 (data_schema.md 형식)
data/raw/nav_YYYY-MM.csv.gz    수집 원본, 월별 분할
scripts/scrape_klia.py         공시실 수집기
scripts/klia_request.json      공시실 조회 조건
scripts/build_latest.py        원본 → latest.json 변환 (수익률·순위·스파크라인 계산)
.github/workflows/daily_batch.yml   평일 자동 실행
```

## 1. 최초 설정 (웹 브라우저만으로 가능)

1. **GitHub 가입** — github.com (무료)
2. **저장소 만들기** — 우측 상단 `+` → `New repository`
   - Repository name: `va-heatmap` · **Public** 선택 (무료 Pages는 공개 저장소만 가능) → `Create repository`
3. **파일 올리기** — 새 저장소 화면의 `uploading an existing file` 클릭 → 이 폴더 안의 내용 전체를 끌어다 놓기 → `Commit changes`
   - `.github` 폴더처럼 점(.)으로 시작하는 폴더가 안 올라가면: `Add file → Create new file` 에서 이름에
     `.github/workflows/daily_batch.yml` 을 그대로 입력하고 내용을 붙여넣기
4. **웹페이지 켜기** — `Settings → Pages` → Source: `Deploy from a branch`, Branch: `main` / `/(root)` → `Save`
   - 1~2분 후 `https://<아이디>.github.io/va-heatmap/` 에서 열림 (이 단계에선 MOCK 화면)
5. **배치 쓰기 권한** — `Settings → Actions → General → Workflow permissions` → `Read and write permissions` → `Save`

## 2. 실데이터 켜기 (아래 확인 후)

> 켜기 전 확인: ① `--probe` 진단 결과로 컬럼 매핑 완료 ② 협회 이용약관·자동수집/재게시 허용 여부 ③ 사내 준법 검토(외부 공개 시)

1. `Settings → Secrets and variables → Actions → Variables` 탭 → `New repository variable`
   - Name: `LIVE_DATA` · Value: `true`
2. **과거 이력 적재(1회)** — `Actions → daily-nav-batch → Run workflow`
   - backfill_start / backfill_end 에 **6개월씩** 나눠 입력 (1회 실행 최대 약 5.5시간)
   - 3Y 수익률까지 쓰려면 3년 + 여유분 → 7번 정도 나눠 실행
3. 이후는 자동: 평일 오전 10:37(KST)에 전 영업일 기준가 반영

**끄기**: 변수 `LIVE_DATA` 를 `false` 로 바꾸면 수집 중단 (이미 올라간 데이터는 그대로 공개 유지)

## 3. 수동 실행 (개인 PC)

```bash
pip install pandas openpyxl requests lxml
python scripts/scrape_klia.py --probe --date 2026-11-11   # 공시실 화면 구조 진단
python scripts/scrape_klia.py --date 2026-11-11           # 1일치 수집 → data/raw/
python scripts/build_latest.py data/raw -o data/latest.json
python -m http.server 8000                                # 브라우저에서 http://localhost:8000
```
(index.html 을 파일로 더블클릭하면 브라우저 보안상 data/latest.json 을 못 읽어 MOCK 이 나옴 → 위 마지막 줄로 열 것)

## 고지
- 수익률은 기준가 기준(특별계정 운용보수 차감 후)이며 계약자 적립금 수익률과 다름
- 과거 수익률은 미래 수익을 보장하지 않음. 특정 펀드 추천·펀드변경 권유 아님
- 데이터 출처: 생명보험협회 공시실. 비공식 개인 제작 사이트
