#!/usr/bin/env bash
# Запуск приложения экспертной валидации LegalGraph
set -e
cd "$(dirname "$0")"

# 1. Зависимости (единожды)
if ! python3 -c "import fastapi, uvicorn, jinja2" 2>/dev/null; then
    echo "→ Устанавливаю зависимости..."
    python3 -m pip install --user -q -r requirements.txt
fi

# 2. Инициализация БД + загрузка статей из mvp_output/ + создание admin/admin (единожды)
if [ ! -f data/annotations.db ]; then
    echo "→ Инициализирую БД и загружаю статьи..."
    python3 seed.py
fi

# 3. Запуск сервера
PORT=${PORT:-8080}
echo ""
echo "════════════════════════════════════════════════════════"
echo "  LegalGraph • Экспертная валидация"
echo "  → http://localhost:${PORT}"
echo "  → Логин по умолчанию: admin / admin"
echo "  → Ctrl+C для остановки"
echo "════════════════════════════════════════════════════════"
echo ""
exec python3 -m uvicorn app:app --host 0.0.0.0 --port "$PORT" --reload
