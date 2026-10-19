#!/bin/zsh
# 맥북 1회 설정. 터미널에서:  zsh ~/Claude/kosdaq_db/setup_mac.sh
set -e
cd "$(dirname "$0")"
echo "== Python 패키지"; python3 -m pip install --user -q -r requirements.txt
echo "== Playwright Chromium"; python3 -m playwright install chromium
if ! command -v brew >/dev/null; then echo "Homebrew 없음 → https://brew.sh 에서 설치 후 다시 실행"; exit 1; fi
command -v gh >/dev/null || brew install gh
command -v wrangler >/dev/null || npm install -g wrangler
[ -f config.json ] || { echo '{"dart_api_key": "여기에_DART_키", "krx_api_key": "여기에_KRX_키"}' > config.json; echo "config.json 생성됨 → 키를 채워 넣으세요"; }
echo "== 스모크 테스트"; python3 smoke_test.py
echo
echo "다음 단계 (직접):  gh auth login   /   wrangler login   /   config.json 키 입력"
echo "그 다음:           python3 run.py init --limit 5"
