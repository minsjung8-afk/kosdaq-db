# kosdaq_db — 코스닥 상장사 심사참고 DB (틀 v0.1)

## 구성
```
kosdaq_db/
├─ schema.sql          # SQLite 스키마 (개요·사업내용·재무·주가·공시·리포트메타)
├─ common.py           # 설정·DB·DART 호출 래퍼(호출간격/재시도/한도 감지)
├─ collect/
│   ├─ listing.py      # KRX 코스닥 목록(FinanceDataReader) + DART corpCode 매핑 + 기업개황
│   ├─ financials.py   # fnlttSinglAcntAll(연결/별도) 적재 → fin_kpi 산출
│   ├─ business.py     # document.xml → 'II. 사업의 내용' 추출, 감사의견, 최대주주, 주요공시 필터
│   └─ prices.py       # 일봉(FinanceDataReader) + 발행주식수(DART) → 시총·52주·변동성·거래대금
├─ summarize.py        # 사업의 내용 → 심사용 정형요약 JSON (Claude API, 선택)
├─ profile.py          # 기업별 프로필 Markdown 출력 (profiles/*.md)
├─ run.py              # 진입점
└─ smoke_test.py       # DART 없이 파서·KPI·렌더 검증 (python smoke_test.py)
```

## 준비 (PC)
```bash
pip install requests finance-datareader anthropic
set DART_API_KEY=발급키          # 또는 config.json {"dart_api_key": "..."}
set ANTHROPIC_API_KEY=...        # 요약 단계만 필요
```

## 실행 순서 (샘플 5개사로 먼저)
```bash
python run.py init  --limit 5
python run.py fin   --limit 5 --years 2023 2024 2025
python run.py biz   --limit 5 --year 2025
python run.py price --limit 5 --year 2025
python run.py summarize --limit 5          # 선택
python run.py profile
```
`fetch_log` 테이블에 성공/실패가 남는다. `sqlite3 kosdaq.db "select job,status,count(*) from fetch_log group by 1,2"`.

## 호출량 추산 (코스닥 ~1,750사 기준)
| 단계 | 호출/사 | 합계 |
|---|---|---|
| 개황 | 1 | 1,750 |
| 재무 3개년×연결/별도 | 6 | 10,500 |
| 사업보고서 원문+공시목록+감사의견+주주+주식수 | 5~6 | ~10,000 |
DART 일일 한도(키당 1만 건 내외) 때문에 **전수 적재는 2~3일에 나눠** 돌린다. 분기 갱신은 재무·주가만 돌리면 된다.

## 알려진 한계 / 다음 단계
- `fin_kpi.borrowings`는 계정명 키워드 합산(근사). 표준계정 미사용 기업은 누락될 수 있음 → 주석 "차입금 현황" 파싱으로 보강 예정
- `business.full_text`는 표가 ` | ` 구분 텍스트로 평탄화됨. 매출구성 표 등은 LLM 요약 단계에서 복원
- 리포트(`report_meta`)는 KIND 기술분석보고서 수집기 미작성 — 접속 가능 확인 후 추가
- 심사통합뷰/사전협의서 양식과 연결: `profile.render()` 출력 필드를 체크리스트 ⑥ 재무현황·담보(상장주식) 항목에 매핑

## v0.2 변경
- 목록·시세를 **KRX Open API**로 교체(`collect/krx.py`). 시세는 날짜별 전종목 1호출 → 1년치 ≈ 250호출로 전시장 적재. 시총·발행주식수도 KRX에서 받아 DART 주식총수 호출 제거
- `collect/kind_ir.py`: KIND IR자료실(기업설명회 PDF 링크) 수집, 선택 실행. 클라우드 IP는 차단되므로 PC에서
- `build_site.py` + `site_template/index.html`: 정적 사이트(검색 + 6개 섹션 프로필 + 1년 주가차트). `python run.py site`
- `deploy.bat` / `deploy.sh`: 빌드 후 `wrangler pages deploy`

## Cloudflare Pages 배포
```bash
npm install -g wrangler
wrangler login                      # 브라우저로 1회 로그인
deploy.bat                          # = python run.py site && wrangler pages deploy site --project-name kosdaq-db
```
첫 배포 때 프로젝트가 없으면 wrangler가 생성 여부를 물어봄(y). URL은 `https://kosdaq-db.pages.dev`.
접근 제한: Cloudflare 대시보드 → Zero Trust → Access → Applications → Add → Self-hosted → `kosdaq-db.pages.dev` → 정책 "Emails ending in @ibkc.co.kr"(또는 개별 이메일) → OTP. 50명까지 무료.

## config.json 예시
```json
{"dart_api_key": "...", "krx_api_key": "..."}
```

## v0.3 변경 — 리포트 수집·분석, GitHub 운영
- `collect/hankyung.py`: 한경컨센서스 기업리포트. robots.txt 허용(/consensus) 확인. 목록 API는 페이지 토큰으로 보호되므로 Playwright로 실제 페이지를 열어 페이지가 받는 JSON을 읽음(토큰 재현 안 함). DB에 있는 종목만 저장, PDF는 `reports/<종목코드>/`에 개인 PC 보관(깃·사이트 미포함)
- `analyze_report.py`: PDF → Claude → 전망치·밸류에이션·투자포인트·리스크·심사메모(자체 서술). 원문 미저장. `forecast_vs_actual()`로 전망 vs DB 실적 달성률
- 사이트 6번 섹션: 리포트 목록 + 전망/실적 대조표 + 분석 카드
- `.github/workflows/collect.yml`: 매주 토 06:00 KST 시세 갱신 → site 빌드 → kosdaq.db 커밋 → Cloudflare 배포. 수동 실행(workflow_dispatch)에서 job 선택 가능
- `requirements.txt`, `.gitignore`(config.json·reports/·site/ 제외)

### GitHub 설정 (1회)
```bash
cd kosdaq_db
git init && git add . && git commit -m "init"
gh repo create kosdaq-db --private --source . --push     # 또는 github.com에서 비공개 저장소 만들고 remote 추가
```
Settings → Secrets and variables → Actions 에 등록:
`DART_API_KEY`, `KRX_API_KEY`, `ANTHROPIC_API_KEY`(선택), `CLOUDFLARE_API_TOKEN`(Pages Edit 권한), `CLOUDFLARE_ACCOUNT_ID`
Settings → Actions → General → Workflow permissions → **Read and write** (DB 커밋용)

### 역할 분담
| 위치 | 역할 | 들어있는 것 |
|---|---|---|
| GitHub (비공개) | 원본 DB·코드·자동수집 | 코드, kosdaq.db, 워크플로 |
| 맥북 | 작업·리포트 PDF 보관 | 클론 + config.json + reports/ |
| Cloudflare Pages | 열람 | site/ (JSON·HTML만) |

리포트 수집·분석(`hk`, `analyze`)은 Playwright·PDF 보관 때문에 맥북에서 돌리고 `git push` → 다음 Actions 배포 때 반영. 재무(`fin`)·사업내용(`biz`)은 DART 한도 때문에 Actions 수동실행으로 하루 하나씩.
