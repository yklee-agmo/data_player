#!/usr/bin/env bash
# Data Player 실행 래퍼.
#
# 아이콘을 더블클릭해서 실행하면 터미널이 없어 오류가 보이지 않는다.
# 그래서 여기서 PyQt6 가 있는 python 을 찾아 주고, 실패하면 창을 띄워 알려 준다.

set -uo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
APP="$HERE/data_player.py"

die() {
    printf '%s\n' "$1" >&2
    if command -v zenity >/dev/null 2>&1; then
        zenity --error --no-wrap --width=460 --title="Data Player" --text="$1" 2>/dev/null
    fi
    exit 1
}

has_pyqt() { "$1" -c 'import PyQt6.QtWidgets' >/dev/null 2>&1; }

[ -f "$APP" ] || die "data_player.py 를 찾을 수 없습니다:\n$APP"

# 설치할 때 찾아 둔 python 을 먼저 쓰고, 없으면 흔한 위치를 차례로 확인한다.
PY=""
for cand in "${DATA_PLAYER_PYTHON:-}" \
            "$(cat "$HERE/.python-path" 2>/dev/null)" \
            "$HOME/anaconda3/bin/python3" \
            "$HOME/miniconda3/bin/python3" \
            python3; do
    [ -n "$cand" ] || continue
    command -v "$cand" >/dev/null 2>&1 || continue
    if has_pyqt "$cand"; then PY="$cand"; break; fi
done

[ -n "$PY" ] || die "PyQt6 가 설치된 python 을 찾지 못했습니다.\n\n다음 명령으로 설치해 주세요:\n    pip install -r $HERE/requirements.txt"

# 오류로 끝나면 마지막 출력을 창으로 보여 준다.
output=$("$PY" "$APP" "$@" 2>&1)
status=$?
if [ $status -ne 0 ]; then
    die "Data Player 가 오류로 종료되었습니다 (코드 $status).\n\n$(printf '%s' "$output" | tail -c 1200)"
fi
