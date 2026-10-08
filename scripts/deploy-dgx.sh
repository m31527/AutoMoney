#!/usr/bin/env bash
# Run on the Mac. Remote credentials/data remain on the DGX.
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
action=${1:-help}
case "$action" in
  check|pull|shadow-btc-eth|shadow-sol-xrp|exit-btc-eth|exit-sol-xrp|testnet-auth|soak-start|soak-status) ;;
  *)
    echo '用法：bash scripts/deploy-dgx.sh {check|pull|shadow-btc-eth|shadow-sol-xrp|exit-btc-eth|exit-sol-xrp|testnet-auth|soak-start|soak-status}'
    echo 'check：只檢查連線；其餘先 pull，再執行指定項目。'
    exit 0 ;;
esac
# Exact local revision must already have been pushed before deployment.
revision=$(git rev-parse HEAD)
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
  m31527@192.168.50.86 bash -s -- "$action" "$revision" <<'REMOTE'
set -euo pipefail
action=$1
expected=$2
cd "$HOME/AutoMoney"
if [[ "$action" == check ]]; then
  printf 'SSH OK\n'
  git log -1 --oneline
  docker compose version
  exit 0
fi
exec 9>"$HOME/.automoney-deploy.lock"
flock -n 9 || { echo '已有部署正在進行'; exit 1; }
[[ $(git branch --show-current) == main ]] || { echo '遠端不是 main，停止部署'; exit 1; }
if [[ -n $(git status --porcelain --untracked-files=no) ]]; then
  echo '遠端有已追蹤檔案的未提交修改，停止部署；不會覆蓋或清除。'
  exit 1
fi
git pull --ff-only origin main
[[ $(git rev-parse HEAD) == "$expected" ]] || {
  echo '遠端與本機版本不同，尚未重啟服務。請確認 commit 已 push 且兩端 main 一致。'
  exit 1
}
case "$action" in
  pull) ;;
  shadow-btc-eth) bash scripts/openteddy-shadow.sh btc-eth ;;
  shadow-sol-xrp) bash scripts/openteddy-shadow.sh sol-xrp ;;
  exit-btc-eth) bash scripts/research-v2.sh start btc-eth ;;
  exit-sol-xrp) bash scripts/research-v2.sh start sol-xrp ;;
  testnet-auth) bash scripts/testnet-acceptance.sh --check-auth ;;
  soak-start) bash scripts/testnet-soak.sh start ;;
  soak-status) bash scripts/testnet-soak.sh status ;;
esac
printf '部署指令完成：%s\n' "$action"
git log -1 --oneline
REMOTE
