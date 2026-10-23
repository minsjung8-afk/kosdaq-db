# 스케줄러 Worker — 설치 순서 (약 10분, Mac에서)

GitHub Actions의 `schedule`은 혼잡 시 수 시간 지연되거나 아예 빠집니다(10/22 06:00 → 06:57, 10/23 00:10 catchup 미실행).
Cloudflare Worker Cron은 정시 실행이 보장되므로, Worker가 정시에 `workflow_dispatch`로 collect.yml을 호출하게 합니다.
Workers 무료 플랜(일 10만 요청) 안에서 하루 3~4회 호출이라 비용 0.

## 1. GitHub 토큰 만들기 (브라우저)
GitHub → Settings → Developer settings → Fine-grained tokens → Generate new token
- Repository access: **Only select repositories → kosdaq-db**
- Permissions → Repository permissions → **Actions: Read and write** (그 외 전부 No access)
- Expiration: 1년. 생성 직후 값 복사(다시 못 봄). **채팅·파일에 붙여넣지 말 것.**

## 2. Worker 배포 (터미널)
```bash
cd ~/Claude/kosdaq_db/worker
npx --yes wrangler@3 login                      # 브라우저 인증 1회
npx wrangler@3 secret put GH_TOKEN              # 프롬프트에 1번 토큰 붙여넣기(화면에 안 보임)
npx wrangler@3 secret put RUN_KEY               # 수동 실행용 임의 비밀번호(아무 문자열)
npx wrangler@3 deploy
```
배포 후 출력되는 `https://kosdaq-db-scheduler.<계정>.workers.dev` 를 열면 매핑표(JSON)가 보이면 정상.

## 3. 동작 확인
```bash
curl -X POST -H "X-Auth: <RUN_KEY>" "https://kosdaq-db-scheduler.<계정>.workers.dev/run?job=price"
```
→ `dispatched` 가 나오고 GitHub Actions에 price 실행이 뜨면 완료. 로그: Cloudflare 대시보드 → Workers → kosdaq-db-scheduler → Logs.

## 4. GitHub cron 끄기(선택)
Worker가 안정적으로 돌면 `.github/workflows/collect.yml`의 `schedule:` 블록을 지워 이중 실행을 막습니다(패치로 처리 예정).
그 전까지는 둘 다 켜 두어도 무방 — concurrency 그룹이 같은 job 중복을 대기열로 막습니다.

## 운영 메모
- 시간을 바꾸려면 `wrangler.toml`의 crons와 `scheduler.js`의 TABLE을 **같이** 수정(문자열 일치 필요) 후 `npx wrangler@3 deploy`.
- 토큰 만료 시 `npx wrangler@3 secret put GH_TOKEN` 으로 교체.
