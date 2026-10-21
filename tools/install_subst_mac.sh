#!/bin/bash
# 자택 Mac 1회 설치: 평일 06:45 자동 깨우기(pmset) + 06:50 대용가 수집·R2 업로드(launchd)
#   사용: bash tools/install_subst_mac.sh   (kosdaq_db 폴더에서)
# 사전에 ~/.kosdaq_r2.env 를 만들어 둘 것 (키는 본인이 직접 입력):
#   CLOUDFLARE_ACCOUNT_ID=...
#   R2_ACCESS_KEY_ID=...
#   R2_SECRET_ACCESS_KEY=...
#   R2_BUCKET=kosdaq-db
set -e
REPO="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$HOME/.kosdaq_r2.env" ] || { echo "~/.kosdaq_r2.env 가 없습니다. 먼저 만들어 주세요."; exit 1; }
chmod 600 "$HOME/.kosdaq_r2.env"
command -v aws >/dev/null || { echo "aws cli 설치 중..."; brew install awscli; }
python3 -c "import requests" 2>/dev/null || python3 -m pip install --user requests
PY="$(command -v python3)"
PLIST="$HOME/Library/LaunchAgents/com.kosdaq.subst.plist"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.kosdaq.subst</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$REPO/tools/subst_local.py</string></array>
  <key>StartCalendarInterval</key><array>
    <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>6</integer><key>Minute</key><integer>50</integer></dict>
    <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>6</integer><key>Minute</key><integer>50</integer></dict>
    <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>6</integer><key>Minute</key><integer>50</integer></dict>
    <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>6</integer><key>Minute</key><integer>50</integer></dict>
    <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>6</integer><key>Minute</key><integer>50</integer></dict>
  </array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>StandardOutPath</key><string>$HOME/kosdaq_subst/launchd.out</string>
  <key>StandardErrorPath</key><string>$HOME/kosdaq_subst/launchd.err</string>
</dict></plist>
EOF
mkdir -p "$HOME/kosdaq_subst"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "launchd 등록 완료 (평일 06:50). 자동 깨우기 예약(관리자 비밀번호 필요):"
sudo pmset repeat wakeorpoweron MTWRF 06:45:00
echo "즉시 1회 테스트 실행:"
"$PY" "$REPO/tools/subst_local.py"
