# 검증계·운영계 설정 안내 (Cloudflare Pages)

## 구조
| 구분 | 브랜치 | 주소(예시) | 접근 |
|---|---|---|---|
| 운영계 | `main` | `https://va-heatmap.pages.dev` | 누구나. 하단 '관리자 권한요청' 링크 |
| 검증계 | `develop` | `https://develop.va-heatmap.pages.dev` | 승인된 관리자만(사번 + 본인 비밀번호) |

- 수정 요청은 `develop` 에만 반영 → 검증계에서 확인 → 상단 띠의 **이관** 버튼 → `develop` 이 `main` 으로 병합 → 운영계 자동 배포(1~2분)
- 일 배치 데이터는 `main` 에만 쌓이고, 검증계는 운영계 데이터를 그대로 보여 줌
- 관리 화면: 검증계 `/admin/` (마스터 관리자: 권한 승인·반려, 해제, 접속코드 재발급, 마스터 인계, 처리 이력)
- 권한 신청 정보(소속·사번·이름)와 관리자 목록은 Cloudflare D1(비공개 DB)에만 저장. 공개 저장소에는 저장하지 않음
- 로그인: 사번 + 본인 비밀번호. 승인·초기화 시 마스터가 임시 접속코드를 전달 → 첫 로그인 때 본인 비밀번호 설정(10자 이상, 영문·숫자). 비밀번호는 PBKDF2, 임시 코드는 해시로만 저장. 분실 시 마스터가 '비밀번호 초기화'
- 운영/검증 구분: 요청 주소가 `PROD_HOST` 와 같으면 운영계, 그 외 모든 주소(develop·미리보기)는 검증계로 보고 잠금

## 처음 설정 (약 20분)
1. **Cloudflare 가입**: dash.cloudflare.com
2. **Pages 프로젝트 만들기**: Workers & Pages → 만들기 → Pages → *Git에 연결* → GitHub 권한 허용 → `va-heatmap` 저장소 선택
   - 프로젝트 이름 = 주소 (`이름.pages.dev`). 개인 ID가 드러나지 않는 이름 권장
   - 프로덕션 브랜치 `main`, 프레임워크 *없음*, 빌드 명령 *비움*, 빌드 출력 디렉터리 `/`
3. **D1 데이터베이스**: Workers & Pages → D1 → 데이터베이스 만들기 (이름 예: `va-heatmap-db`). 표는 첫 사용 때 자동 생성
4. **프로젝트 설정 → 바인딩**: D1 데이터베이스 추가, 변수 이름 **`DB`** → 위 DB 선택 (**프로덕션·미리보기 둘 다**)
5. **프로젝트 설정 → 변수 및 비밀** (프로덕션·미리보기 둘 다 같은 값)
   | 이름 | 종류 | 값 |
   |---|---|---|
   | `PROD_HOST` | 텍스트 | 운영 주소의 호스트만 (예: `va-heatmap.pages.dev`) |
   | `GITHUB_REPO` | 텍스트 | `robin951205/va-heatmap` |
   | `SETUP_KEY` | 비밀 | 직접 정한 긴 임의 문자열(최초 마스터 지정용) |
   | `GITHUB_TOKEN` | 비밀 | 아래 6번 토큰 |
6. **GitHub 토큰**: GitHub → Settings → Developer settings → Personal access tokens → *Fine-grained tokens* → Generate
   - Repository access: *Only select repositories* → `va-heatmap`
   - Permissions → Repository → **Contents: Read and write** (그 외 없음)
   - 만료일 1년 권장(만료 전 재발급 → 5번 값 교체)
7. **설정 → 빌드 → 분기 제어**: 미리보기 분기 *사용자 지정* → 포함 `develop`
8. **배포 다시 실행**: 배포 목록에서 `main`, `develop` 최신 배포 *재시도* (바인딩·변수 적용)
9. **최초 마스터 지정**: `https://develop.<프로젝트>.pages.dev/admin/setup` → 설정키(SETUP_KEY)·소속·사번·이름 → 나오는 **임시 접속코드**로 로그인 → 본인 비밀번호 설정
10. 운영 주소를 Claude 에게 알려 주면 기존 GitHub 주소 → 새 주소 자동 연결을 설정

## 운영 메모
- 마스터 인계: 관리 화면 → 마스터 관리자 인계 (본인 비밀번호 재입력). 인계 후 본인은 일반관리자
- 이관 충돌(드묾): 이관 버튼이 '충돌' 안내 → 개발 담당(Claude)에게 요청
- 로그인 실패 8회/15분 → 15분 잠금. 세션 12시간
- 개인정보 보관기간 5년(2026-11-14 결정): 승인·반려 처리일 / 권한 해제일 / 이력 기록일로부터 5년 지나면 자동 파기(마스터가 관리 화면을 열 때 실행). 그 전에도 반려·처리 건은 '기록 삭제' 가능
