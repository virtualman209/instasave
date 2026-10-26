#!/usr/bin/env bash
# Cập nhật InstaSaved: tải code mới nhất từ GitHub rồi build lại container.
# Dữ liệu (ảnh, database, phiên đăng nhập) nằm trong DATA_PATH nên không bị ảnh hưởng.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "⚠  Đã tạo file .env – hãy sửa APP_PASSWORD và DATA_PATH rồi chạy lại ./update.sh"
  exit 1
fi

if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi

echo "→ Tải code mới từ GitHub…"
OLD=$(git rev-parse --short HEAD)
git fetch --quiet origin
BRANCH=$(git rev-parse --abbrev-ref HEAD)
git reset --hard --quiet "origin/$BRANCH"
NEW=$(git rev-parse --short HEAD)
chmod +x update.sh

if [ "$OLD" = "$NEW" ] && [ "${1:-}" != "--force" ]; then
  echo "✓ Đã là bản mới nhất ($NEW). Đảm bảo container đang chạy…"
  $DC up -d
else
  echo "→ Cập nhật $OLD → $NEW:"
  git log --oneline "$OLD..$NEW" 2>/dev/null | sed 's/^/   /' || true
  echo "→ Build lại container (lấy luôn bản instaloader mới nhất)…"
  $DC build --pull --no-cache
  $DC up -d
  docker image prune -f >/dev/null 2>&1 || true
fi

PORT=$(grep -E '^PORT=' .env | cut -d= -f2); PORT=${PORT:-8088}
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo "✓ Xong. Mở http://${IP:-IP-mini-pc}:${PORT}"
