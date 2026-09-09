#!/usr/bin/env bash
# Data Player 를 데스크톱에 등록한다 (아이콘 더블클릭으로 실행).
#
#   ./install.sh              설치
#   ./install.sh --no-mime    설치하되 동영상 파일 연결은 하지 않음
#   ./install.sh --uninstall  제거
#
# 건드리는 곳은 전부 사용자 홈 아래이고 sudo 가 필요 없다.

set -uo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
APP_ID="data-player"
WITH_MIME=1
[ "${1:-}" = "--no-mime" ] && WITH_MIME=0
APPS_DIR="$HOME/.local/share/applications"
ICON_ROOT="$HOME/.local/share/icons/hicolor"
DESKTOP_FILE="$APPS_DIR/$APP_ID.desktop"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
SHORTCUT="$DESKTOP_DIR/$APP_ID.desktop"

info() { printf '  %s\n' "$1"; }

# --------------------------------------------------------------- 제거
if [ "${1:-}" = "--uninstall" ]; then
    echo "Data Player 제거 중..."
    rm -f "$DESKTOP_FILE" "$SHORTCUT" "$HERE/.python-path"
    for size in 16 24 32 48 64 128 256 512; do
        rm -f "$ICON_ROOT/${size}x${size}/apps/$APP_ID.png"
    done
    rm -f "$ICON_ROOT/scalable/apps/$APP_ID.svg"
    command -v update-desktop-database >/dev/null && update-desktop-database "$APPS_DIR" 2>/dev/null
    command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -f -t "$ICON_ROOT" 2>/dev/null
    info "제거했습니다. (프로젝트 파일은 그대로 있습니다)"
    exit 0
fi

echo "Data Player 설치 중..."

# --------------------------------------------- 1. PyQt6 가 있는 python 찾기
PY=""
for cand in "${DATA_PLAYER_PYTHON:-}" python3 \
            "$HOME/anaconda3/bin/python3" "$HOME/miniconda3/bin/python3"; do
    [ -n "$cand" ] || continue
    command -v "$cand" >/dev/null 2>&1 || continue
    if "$cand" -c 'import PyQt6.QtWidgets' >/dev/null 2>&1; then
        PY="$(command -v "$cand")"; break
    fi
done
if [ -z "$PY" ]; then
    echo "  경고: PyQt6 가 설치된 python 을 찾지 못했습니다." >&2
    echo "        pip install -r $HERE/requirements.txt  후 다시 실행하세요." >&2
else
    printf '%s' "$PY" > "$HERE/.python-path"
    info "python: $PY"
    "$PY" -c 'import imageio_ffmpeg' >/dev/null 2>&1 \
        || echo "  참고: imageio-ffmpeg 가 없어 동영상 재생은 안 됩니다 (이미지 폴더는 정상)." >&2
fi

chmod +x "$HERE/launch.sh" "$HERE/data_player.py" 2>/dev/null

# --------------------------------------------------------- 2. 아이콘 설치
for size in 16 24 32 48 64 128 256 512; do
    src="$HERE/assets/icon_${size}.png"
    [ -f "$src" ] || continue
    mkdir -p "$ICON_ROOT/${size}x${size}/apps"
    cp -f "$src" "$ICON_ROOT/${size}x${size}/apps/$APP_ID.png"
done
if [ -f "$HERE/assets/$APP_ID.svg" ]; then
    mkdir -p "$ICON_ROOT/scalable/apps"
    cp -f "$HERE/assets/$APP_ID.svg" "$ICON_ROOT/scalable/apps/$APP_ID.svg"
fi
info "아이콘: $ICON_ROOT/*/apps/$APP_ID.png"

# ------------------------------------------------------ 3. .desktop 만들기
mkdir -p "$APPS_DIR"
if [ "$WITH_MIME" -eq 1 ]; then
    MIME_LINE="MimeType=inode/directory;video/mp4;video/x-msvideo;video/quicktime;video/x-matroska;video/webm;video/mpeg;video/x-ms-wmv;video/x-flv;"
else
    MIME_LINE="# MimeType 등록 안 함 (--no-mime)"
fi
cat > "$DESKTOP_FILE" <<DESKTOP
[Desktop Entry]
Type=Application
Version=1.0
Name=Data Player
Name[ko]=데이터 플레이어
GenericName=Image Sequence and Video Player
GenericName[ko]=이미지 시퀀스 · 동영상 플레이어
Comment=Play an image folder or a video frame by frame, with speed control
Comment[ko]=이미지 폴더나 동영상을 배속을 바꿔 가며 프레임 단위로 재생합니다
Exec=$HERE/launch.sh %f
Icon=$APP_ID
Terminal=false
Categories=AudioVideo;Video;Player;
Keywords=video;image;sequence;frame;player;동영상;이미지;재생;
${MIME_LINE}
StartupNotify=true
StartupWMClass=data_player.py
DESKTOP
chmod 644 "$DESKTOP_FILE"

if command -v desktop-file-validate >/dev/null 2>&1; then
    if desktop-file-validate "$DESKTOP_FILE"; then
        info "메뉴 항목: $DESKTOP_FILE (검증 통과)"
    else
        echo "  경고: .desktop 검증 실패" >&2
    fi
fi

# ------------------------------------------------ 4. 바탕화면 아이콘 놓기
if [ -d "$DESKTOP_DIR" ]; then
    cp -f "$DESKTOP_FILE" "$SHORTCUT"
    chmod +x "$SHORTCUT"
    # GNOME 은 신뢰 표시가 없으면 바탕화면 실행 아이콘을 텍스트 파일로 취급한다.
    if command -v gio >/dev/null 2>&1; then
        gio set "$SHORTCUT" metadata::trusted true 2>/dev/null
        gio set "$SHORTCUT" metadata::nautilus-icon-position "" 2>/dev/null
    fi
    info "바탕화면: $SHORTCUT"
else
    echo "  참고: 바탕화면 폴더($DESKTOP_DIR)가 없어 메뉴에만 등록했습니다." >&2
fi

# ------------------------------------------------------------ 5. 캐시 갱신
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS_DIR" 2>/dev/null
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -f -t "$ICON_ROOT" 2>/dev/null

echo
echo "설치가 끝났습니다."
echo "  · 바탕화면의 'Data Player' 아이콘을 더블클릭하세요."
echo "  · 또는 Activities(윈도우 키)에서 'Data Player' 를 검색하세요."
if [ "$WITH_MIME" -eq 1 ]; then
    echo "  · 동영상 파일을 우클릭 → '다른 프로그램으로 열기' 에도 나타납니다."
    cur="$(xdg-mime query default video/mp4 2>/dev/null)"
    if [ "$cur" = "$APP_ID.desktop" ]; then
        echo
        echo "  참고: 이 PC 에 다른 동영상 플레이어가 없어서 Data Player 가"
        echo "        mp4 등의 '기본 프로그램' 이 되었습니다. 원치 않으면:"
        echo "            ./install.sh --uninstall && ./install.sh --no-mime"
    fi
fi
