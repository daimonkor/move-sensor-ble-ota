#!/bin/sh
# Подставить GitHub-логин вместо OWNER: ./tools/set_github.sh <login> [repo]
set -e
[ -n "$1" ] || { echo "usage: $0 <github-login> [repo]"; exit 1; }
OWNER="$1"; REPO="${2:-move-sensor-ble-ota}"
cd "$(dirname "$0")/.."
INT=custom_components/move_sensor_ble_ota
for f in README.md $INT/docs/index.html $INT/docs/index.uk.html $INT/docs/index.en.html; do
  sed -i.tmp -e "s#OWNER/move-sensor-ble-ota#$OWNER/$REPO#g" \
             -e "s#owner=OWNER#owner=$OWNER#g" \
             -e "s#repository=move-sensor-ble-ota#repository=$REPO#g" "$f" && rm -f "$f.tmp"
done
python3 - "$OWNER" "$REPO" "$INT/manifest.json" <<'PY'
import json,sys
o,r,p=sys.argv[1:]
m=json.load(open(p))
m["codeowners"]=["@"+o]
json.dump(m,open(p,"w"),indent=2,ensure_ascii=False); open(p,"a").write("\n")
PY
grep -rn "OWNER" README.md $INT/docs/index.html $INT/docs/index.uk.html $INT/docs/index.en.html || echo "OK: OWNER заменён на $OWNER"
