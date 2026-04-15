#!/usr/bin/env bash
# Автоматический деплой LegalGraph Validator на Railway.
# Требования:
#   1. railway CLI установлен: brew install railway
#   2. Выполнен `railway login` (один раз — откроется браузер).
#
# Скрипт идемпотентен: безопасно перезапускать.

set -e
cd "$(dirname "$0")"

echo "════════════════════════════════════════════════════════"
echo "  LegalGraph Validator → Railway"
echo "════════════════════════════════════════════════════════"
echo

# ---------- 0. Проверка авторизации ----------
if ! railway whoami >/dev/null 2>&1; then
    echo "✗ Не авторизованы в Railway."
    echo "  Выполните: railway login  (откроется браузер)"
    echo "  или:      railway login --browserless  (выдаст код для вставки в браузере)"
    exit 1
fi
echo "✓ Railway: $(railway whoami 2>&1 | grep -o '[^ ]*@[^ ]*\|Logged.*' | head -1)"
echo

# ---------- 1. Инициализация проекта (если ещё не привязан) ----------
if [ ! -f ".railway/project.json" ] && ! railway status >/dev/null 2>&1; then
    PROJECT_NAME="${RAILWAY_PROJECT_NAME:-legalgraph-validator}"
    echo "→ Создаю новый проект Railway: $PROJECT_NAME"
    railway init --name "$PROJECT_NAME"
else
    echo "✓ Проект уже привязан: $(railway status 2>&1 | grep -i 'project' | head -1)"
fi
echo

# ---------- 2. Переменные окружения ----------
echo "→ Настройка переменных окружения:"
ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
# Если ADMIN_PASSWORD не задан в env при запуске скрипта — генерируем сильный
if [ -z "${ADMIN_PASSWORD:-}" ]; then
    ADMIN_PASSWORD=$(python3 -c "import secrets,string; print(''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(24)))")
    echo "  ⚠ ADMIN_PASSWORD не задан — сгенерирован: $ADMIN_PASSWORD"
    echo "  (сохраните этот пароль, он нужен для первого входа в /login)"
fi

railway variables --set "ADMIN_USERNAME=$ADMIN_USERNAME" --skip-deploys
railway variables --set "ADMIN_PASSWORD=$ADMIN_PASSWORD" --skip-deploys
railway variables --set "DATA_DIR=/app/data" --skip-deploys
echo "  ✓ ADMIN_USERNAME, ADMIN_PASSWORD, DATA_DIR"
echo

# ---------- 3. Volume для SQLite ----------
echo "→ Проверяю Volume для постоянного хранения БД:"
if ! railway volume list 2>&1 | grep -q "/app/data"; then
    echo "  → Создаю Volume с mount /app/data"
    railway volume add --mount-path /app/data || {
        echo "  ⚠ Не удалось через CLI. Создайте вручную в UI:"
        echo "     Railway → Service → Settings → Volumes → Mount: /app/data"
    }
else
    echo "  ✓ Volume уже существует"
fi
echo

# ---------- 4. Деплой ----------
echo "→ Деплой (railway up)..."
railway up --ci --detach
echo "  ✓ Загружено, сборка запущена"
echo

# ---------- 5. Публичный домен ----------
echo "→ Настраиваю публичный домен:"
DOMAIN_OUT=$(railway domain 2>&1 || true)
echo "$DOMAIN_OUT" | head -5
echo

# ---------- Итог ----------
echo "════════════════════════════════════════════════════════"
echo "  ✓ Деплой запущен"
echo "════════════════════════════════════════════════════════"
echo "  Вход:      логин = $ADMIN_USERNAME"
echo "             пароль = $ADMIN_PASSWORD"
echo
echo "  Логи билда:  railway logs --deployment"
echo "  Статус:      railway status"
echo "  Переменные:  railway variables"
echo "  Открыть:     railway open"
echo "════════════════════════════════════════════════════════"
