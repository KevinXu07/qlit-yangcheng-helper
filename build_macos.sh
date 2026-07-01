#!/usr/bin/env bash
# qlit-flet macOS 打包脚本
# 产物：dist/QLIT养成教育助手.app
#
# 不用 flet pack，因为 flet pack 用 onefile + --add-data mitmproxy.app，
# PyInstaller 会把 mitmproxy.app 解到临时目录后试图签名其内部 binary，
# 但 mitmproxy 是带 framework 签名的 .app bundle，PyInstaller 不认导致失败。
#
# 这里直接用 pyinstaller build.spec（onedir + BUNDLE），mitmproxy.app 在打包后
# 用 ditto 单独拷入 Contents/Resources/，保留原签名结构。
set -euo pipefail
cd "$(dirname "$0")"

APP_NAME="QLIT养成教育助手"
DIST_APP="dist/${APP_NAME}.app"
DIST_ZIP="dist/${APP_NAME}.zip"
MITM_CASKROOM="/opt/homebrew/Caskroom/mitmproxy"
FLET_CACHE_ROOT="${HOME}/.flet/client/flet-desktop-full-0.85.3"
FLET_SRC_APP="${FLET_CACHE_ROOT}/Flet.app"
FLET_BUILD_ROOT=""
FLET_ARCHIVE=""

echo "==> 激活 venv"
[ -f .venv/bin/activate ] && source .venv/bin/activate || {
  echo "✗ 未找到 .venv，请先：python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
  exit 1
}

# ── 签名身份：发给别人用时优先 Developer ID；本机调试才退到 Apple Development / ad-hoc ──
SIGN_IDENTITY=$(/usr/bin/security find-identity -v -p codesigning 2>/dev/null \
  | sed -n 's/.*"\(Developer ID Application:.*\)".*/\1/p' | head -1)
SIGN_KIND="developer-id"
if [ -n "$SIGN_IDENTITY" ]; then
  echo "✓ 使用 Developer ID 证书：$SIGN_IDENTITY"
  SIGN_FLAG=("--sign" "$SIGN_IDENTITY")
else
  SIGN_IDENTITY=$(/usr/bin/security find-identity -v -p codesigning 2>/dev/null \
    | sed -n 's/.*"\(Apple Development:.*\)".*/\1/p' | head -1)
  SIGN_KIND="apple-development"
  if [ -n "$SIGN_IDENTITY" ]; then
    echo "⚠ 未找到 Developer ID，退回 Apple Development：$SIGN_IDENTITY"
    echo "  这个包适合本机/同开发账号设备调试；发给同学建议用 Developer ID + notarize。"
    SIGN_FLAG=("--sign" "$SIGN_IDENTITY")
  else
    SIGN_KIND="ad-hoc"
    echo "⚠ 未找到代码签名证书，退回 ad-hoc（别人机器上大概率打不开）"
    SIGN_FLAG=("--sign" "-")
  fi
fi

log_cmd() {
  local label="$1"
  shift
  local tmp
  tmp="$(mktemp)"
  if "$@" >"$tmp" 2>&1; then
    sed "s/^/  ${label}: /" "$tmp"
    rm -f "$tmp"
    return 0
  fi
  local status=$?
  sed "s/^/  ${label}: /" "$tmp"
  rm -f "$tmp"
  return "$status"
}

codesign_item() {
  local path="$1"
  [ -e "$path" ] || return 0
  if [ "$SIGN_KIND" = "developer-id" ]; then
    log_cmd "codesign" /usr/bin/codesign --force "${SIGN_FLAG[@]}" --options runtime --timestamp "$path"
  else
    log_cmd "codesign" /usr/bin/codesign --force "${SIGN_FLAG[@]}" "$path"
  fi
}

is_macho() {
  file "$1" 2>/dev/null | grep -q "Mach-O"
}

sign_loose_macho_files() {
  local root="$1"
  [ -d "$root" ] || return 0
  while IFS= read -r -d '' file_path; do
    case "$file_path" in
      *.framework/*) continue ;;
    esac
    if is_macho "$file_path"; then
      codesign_item "$file_path"
    fi
  done < <(find "$root" -type f \( -name '*.dylib' -o -name '*.so' -o -perm -111 \) -print0)
}

sign_frameworks() {
  local root="$1"
  [ -d "$root" ] || return 0
  while IFS= read -r -d '' framework_path; do
    codesign_item "$framework_path"
  done < <(find "$root" -type d -name '*.framework' -depth -print0)
}

prepare_flet_runtime_bundle() {
  if [ ! -d "$FLET_SRC_APP" ]; then
    echo "✗ 未找到 Flet 运行时：$FLET_SRC_APP"
    echo "  请先运行一次本项目，或确认 ~/.flet/client 已有 flet-desktop-full-0.85.3/Flet.app"
    exit 1
  fi

  FLET_BUILD_ROOT="$(mktemp -d)"
  local app_bundle_name="${APP_NAME}.app"
  local app_executable_name="$APP_NAME"
  local flet_app="$FLET_BUILD_ROOT/$app_bundle_name"
  local flet_xcassets="$FLET_BUILD_ROOT/AppIcon.xcassets"
  local flet_appiconset="$flet_xcassets/AppIcon.appiconset"
  local flet_build="$FLET_BUILD_ROOT/build"
  local icon_src_png="assets/app-icon.png"
  local original_exec="Flet"

  ditto "$FLET_SRC_APP" "$FLET_BUILD_ROOT/Flet.app"
  mv "$FLET_BUILD_ROOT/Flet.app" "$flet_app"

  if [ -f "$flet_app/Contents/MacOS/$original_exec" ]; then
    mv "$flet_app/Contents/MacOS/$original_exec" "$flet_app/Contents/MacOS/$app_executable_name"
  fi

  plutil -replace CFBundleIconFile -string AppIcon "$flet_app/Contents/Info.plist" 2>/dev/null || true
  plutil -replace CFBundleIconName -string AppIcon "$flet_app/Contents/Info.plist" 2>/dev/null || true
  plutil -replace LSUIElement -bool NO "$flet_app/Contents/Info.plist" 2>/dev/null || true
  plutil -replace CFBundleName -string "$APP_NAME" "$flet_app/Contents/Info.plist" 2>/dev/null || true
  plutil -replace CFBundleDisplayName -string "$APP_NAME" "$flet_app/Contents/Info.plist" 2>/dev/null || true
  plutil -replace CFBundleExecutable -string "$app_executable_name" "$flet_app/Contents/Info.plist" 2>/dev/null || true
  plutil -replace CFBundleIdentifier -string "com.qlit.campusauth.flet" "$flet_app/Contents/Info.plist" 2>/dev/null || true

  if [ -f "$icon_src_png" ]; then
    mkdir -p "$flet_appiconset" "$flet_build"
    make_icon() {
      local size="$1"
      local filename="$2"
      sips -s format png -z "$size" "$size" "$icon_src_png" --out "$flet_appiconset/$filename" >/dev/null
    }

    make_icon 16   "icon_16x16.png"
    make_icon 32   "icon_16x16@2x.png"
    make_icon 32   "icon_32x32.png"
    make_icon 64   "icon_32x32@2x.png"
    make_icon 128  "icon_128x128.png"
    make_icon 256  "icon_128x128@2x.png"
    make_icon 256  "icon_256x256.png"
    make_icon 512  "icon_256x256@2x.png"
    make_icon 512  "icon_512x512.png"
    make_icon 1024 "icon_512x512@2x.png"

    cat > "$flet_appiconset/Contents.json" <<'JSON'
{
  "images" : [
    { "idiom" : "mac", "size" : "16x16",   "scale" : "1x", "filename" : "icon_16x16.png" },
    { "idiom" : "mac", "size" : "16x16",   "scale" : "2x", "filename" : "icon_16x16@2x.png" },
    { "idiom" : "mac", "size" : "32x32",   "scale" : "1x", "filename" : "icon_32x32.png" },
    { "idiom" : "mac", "size" : "32x32",   "scale" : "2x", "filename" : "icon_32x32@2x.png" },
    { "idiom" : "mac", "size" : "128x128", "scale" : "1x", "filename" : "icon_128x128.png" },
    { "idiom" : "mac", "size" : "128x128", "scale" : "2x", "filename" : "icon_128x128@2x.png" },
    { "idiom" : "mac", "size" : "256x256", "scale" : "1x", "filename" : "icon_256x256.png" },
    { "idiom" : "mac", "size" : "256x256", "scale" : "2x", "filename" : "icon_256x256@2x.png" },
    { "idiom" : "mac", "size" : "512x512", "scale" : "1x", "filename" : "icon_512x512.png" },
    { "idiom" : "mac", "size" : "512x512", "scale" : "2x", "filename" : "icon_512x512@2x.png" }
  ],
  "info" : {
    "author" : "xcode",
    "version" : 1
  }
}
JSON

    /Applications/Xcode.app/Contents/Developer/usr/bin/actool \
      --compile "$flet_build" \
      --platform macosx \
      --target-device mac \
      --minimum-deployment-target 11.0 \
      --app-icon AppIcon \
      --output-partial-info-plist "$flet_build/partial-info.plist" \
      "$flet_xcassets" >/dev/null

    if [ -f "$flet_build/AppIcon.icns" ]; then
      ditto "$flet_build/AppIcon.icns" "$flet_app/Contents/Resources/AppIcon.icns"
    fi
    if [ -f "$flet_build/Assets.car" ]; then
      ditto "$flet_build/Assets.car" "$flet_app/Contents/Resources/Assets.car"
    fi
  fi

  codesign_item "$flet_app"

  FLET_ARCHIVE="$FLET_BUILD_ROOT/flet-macos.tar.gz"
  COPYFILE_DISABLE=1 /usr/bin/tar \
    --exclude '._*' \
    --exclude '.DS_Store' \
    -C "$FLET_BUILD_ROOT" \
    -czf "$FLET_ARCHIVE" \
    "$app_bundle_name"
  rm -rf "$flet_app" "$flet_xcassets" "$flet_build"
}

# ── 1. 准备 mitmproxy.app ─────────────────────────────
echo "==> 准备 mitmproxy.app"
if [ ! -d "mitmproxy.app" ]; then
  SRC=$(ls -d "$MITM_CASKROOM"/*/mitmproxy.app 2>/dev/null | head -1)
  if [ -z "$SRC" ]; then
    echo "✗ 未找到 brew 的 mitmproxy.app，请先：brew install --cask mitmproxy"
    exit 1
  fi
  echo "  从 $SRC 复制"
  ditto "$SRC" mitmproxy.app
else
  echo "  已存在，跳过"
fi
if ! ./mitmproxy.app/Contents/MacOS/mitmdump --version 2>&1 | grep -q "Mitmproxy"; then
  echo "✗ mitmproxy.app 损坏，删除后重跑"
  rm -rf mitmproxy.app
  exit 1
fi

# ── 2. PyInstaller（onedir + BUNDLE）────────────────
echo "==> PyInstaller"
rm -rf build dist 2>/dev/null || true
echo "==> 准备 Flet 运行时归档"
prepare_flet_runtime_bundle
QLIT_FLET_BIN_DIR="$FLET_BUILD_ROOT" pyinstaller build.spec --noconfirm
if [ ! -d "$DIST_APP" ]; then
  echo "✗ 打包失败"
  exit 1
fi

# ── 3. ditto 拷 mitmproxy.app 进 .app/Contents/Resources/ ──
echo "==> 内嵌 mitmproxy.app"
ditto mitmproxy.app "$DIST_APP/Contents/Resources/mitmproxy.app"

# ── 4. 先签外层依赖和 mitmproxy 依赖；Flet 内层放到 4.5 改图标后签 ──
echo "==> 分层签名依赖"
sign_loose_macho_files "$DIST_APP/Contents/Frameworks"
sign_frameworks "$DIST_APP/Contents/Frameworks"
sign_loose_macho_files "$DIST_APP/Contents/Resources/mitmproxy.app/Contents/Frameworks"
sign_frameworks "$DIST_APP/Contents/Resources/mitmproxy.app/Contents/Frameworks"
sign_loose_macho_files "$DIST_APP/Contents/Resources/mitmproxy.app/Contents/MacOS"
codesign_item "$DIST_APP/Contents/Resources/mitmproxy.app"

# ── 4.5 外层宿主重签 ─────────────────────────────────
echo "==> 重签宿主 App"
codesign_item "$DIST_APP"

# ── 5. 验证 ──────────────────────────────────────────
echo "==> 验证"
MITM="$DIST_APP/Contents/Resources/mitmproxy.app/Contents/MacOS/mitmdump"
if [ ! -x "$MITM" ]; then
  echo "✗ mitmdump 不在预期路径：$MITM"
  exit 1
fi
if ! "$MITM" --version 2>&1 | grep -q "Mitmproxy"; then
  echo "✗ mitmdump 启动失败"
  exit 1
fi
log_cmd "verify" /usr/bin/codesign --verify --deep --strict --verbose=4 "$DIST_APP"
if [ "$SIGN_KIND" = "developer-id" ]; then
  if /usr/sbin/spctl -a -vv --type execute "$DIST_APP" >/tmp/qlit-spctl.log 2>&1; then
    sed 's/^/  spctl: /' /tmp/qlit-spctl.log
  else
    sed 's/^/  spctl: /' /tmp/qlit-spctl.log
    echo "⚠ Developer ID 已签名，但还没 notarize/staple。发给同学前建议执行："
    echo "  xcrun notarytool submit dist/${APP_NAME}.zip --keychain-profile <profile> --wait"
    echo "  xcrun stapler staple \"$DIST_APP\""
    echo "  ditto -c -k --keepParent \"$DIST_APP\" \"$DIST_ZIP\""
  fi
  rm -f /tmp/qlit-spctl.log
fi

echo "==> 生成 zip"
rm -f "$DIST_ZIP"
ditto -c -k --keepParent "$DIST_APP" "$DIST_ZIP"

echo ""
echo "✓ 完成：$(pwd)/$DIST_APP"
du -sh "$DIST_APP"
du -sh "$DIST_ZIP"
echo ""
if [ "$SIGN_KIND" = "developer-id" ]; then
  echo "⚠️  发给别人前请 notarize 并 staple；未公证时首次仍可能需要【右键 → 打开】。"
else
  echo "⚠️  当前不是 Developer ID 分发签名；别人机器上双击可能无反应或被 Gatekeeper 拦截。"
  echo "   临时测试请让对方先【右键 → 打开】，或在终端运行查看报错："
  echo "   open \"$(pwd)/$DIST_APP\""
fi
