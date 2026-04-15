# LegalGraph Validator

Веб-приложение для проверки автоизвлечённых связей группой юристов-экспертов с последующим экспортом данных в формат, готовый для машинного обучения.

**Production:** развёрнуто на Railway. **Локально:** `bash run.sh` → http://localhost:8080.

## Что умеет

### Для эксперта
- Логин / логаут через сессию (cookie).
- **Дашборд** со списком 20 статей и персональным прогрессом (голосов / всего).
- **Страница аннотации**:
  - Полный текст статьи слева, список размеченных сущностей.
  - Все авто-извлечённые связи справа в виде карточек.
  - Три кнопки: **✓ Подтверждаю** / **✗ Отклоняю** / **✎ Модифицировать** (меняет `relation_type`).
  - Комментарий к каждой связи.
  - Фильтр: все / неоценённые / подтверждённые / отклонённые / модифицированные.
  - Счётчик голосов других экспертов (без раскрытия того, *кто* как голосовал).
  - Навигация «← предыдущая / следующая →» и кнопка «Завершить статью».

### Для администратора
- Все возможности эксперта + **Админка**:
  - Создание / удаление экспертов, сброс паролей.
  - Глобальная статистика: связей, голосов, прогресс по типам связей.
  - **Precision per relation type** (approve / total_voted).
  - **IAA** — Inter-Annotator Agreement:
    - Fleiss' κ глобальный и по каждому типу связи.
    - Попарный Cohen's κ между всеми парами экспертов.
    - Интерпретация (Landis & Koch).
  - **Экспорт для ML** — кнопка, генерирующая файлы в `data/exports/`:
    - `ml_dataset.jsonl` — каждая связь с распределением голосов + gold-метка.
    - `ml_dataset.csv` — то же в CSV для быстрой аналитики.
    - `gold_relations.jsonl` — только подтверждённые (≥ 67 % согласия) — для обучения классификатора.
    - `disputed.jsonl` — связи с низким согласием — кандидаты на active learning.
    - `expert_additions.jsonl` — связи, которые эксперты добавили вручную.
    - `per_expert_stats.csv` — статистика по каждому эксперту.

## Запуск

```bash
cd annotation_app
bash run.sh
```

Первый запуск:
1. Установит зависимости (FastAPI, uvicorn, Jinja2).
2. Создаст БД и загрузит 20 статей из `../mvp_output/`.
3. Создаст пользователя **admin / admin** (обязательно смените пароль в админке).

Открыть: **http://localhost:8080**

## Архитектура

```
annotation_app/
├── app.py             # FastAPI: роуты, аутентификация, API
├── db.py              # SQLite-схема и подключение
├── seed.py            # Загрузка статей + создание admin
├── iaa.py             # Cohen's/Fleiss' κ
├── export.py          # Генерация файлов для ML
├── templates/         # Jinja2 HTML
│   ├── base.html
│   ├── login.html
│   ├── dashboard.html
│   ├── annotate.html  # главная страница валидации
│   └── admin.html
├── static/style.css
├── data/
│   ├── annotations.db # SQLite БД (создаётся автоматически)
│   └── exports/       # файлы экспорта
└── run.sh
```

## Схема БД (кратко)

- `experts` — учётные записи.
- `articles` (20 шт.) — из `mvp_output/*.json`.
- `entities` — размеченные сущности.
- `relations` — автоизвлечённые связи.
- `votes` — голоса экспертов (UNIQUE(expert_id, relation_id) — один голос на связь).
- `expert_additions` — ручные добавления связей.
- `progress` — завершённые статьи.
- `sessions` — cookie-сессии.

## Формат `ml_dataset.jsonl` (для обучения классификатора)

Каждая строка — JSON-объект:
```json
{
  "rel_id": "REL_0001",
  "code": "УК РК",
  "article_number": 41,
  "article_id": 624,
  "article_text": "...",
  "from_entity_id": "...",
  "from_text": "...",
  "from_label": "legal_subject:ФИЗЛИЦО",
  "to_entity_id": "...",
  "to_text": "...",
  "to_label": "action_type:АКТИВНОЕ",
  "relation_type": "СОВЕРШАЕТ",
  "auto_confidence": 0.87,
  "trigger_phrase": "...",
  "algorithm": "ALGO_СОВЕРШАЕТ_v1",
  "source": "auto",
  "votes": [
    {"expert": "lawyer_1", "verdict": "approve", "comment": null},
    {"expert": "lawyer_2", "verdict": "approve", "comment": null},
    {"expert": "lawyer_3", "verdict": "modify",
     "correction_relation_type": "АДРЕСОВАНО_СУБЪЕКТУ", "comment": "норма-адресат, не агент"}
  ],
  "n_votes": 3,
  "n_approve": 2, "n_reject": 0, "n_modify": 1,
  "majority_verdict": "approve",
  "agreement_ratio": 0.667,
  "gold_label": "СОВЕРШАЕТ"
}
```

**Правила gold-метки:**
- majority = `approve` → `gold_label = relation_type`
- majority = `reject` → `gold_label = "NONE"`
- majority = `modify` → `gold_label` = самая частая `correction_relation_type`

## IAA: пороги интерпретации

| κ | Интерпретация |
|---|---|
| < 0.00 | нет согласия |
| 0.00 – 0.20 | ничтожное (slight) |
| 0.21 – 0.40 | слабое (fair) |
| 0.41 – 0.60 | умеренное (moderate) |
| 0.61 – 0.80 | существенное (substantial) |
| 0.81 – 1.00 | почти полное (almost perfect) |

Для продакшн-разметки обычно требуется ≥ 0.60 (substantial).

## Безопасность

- Пароли хранятся в SHA-256 с статической солью (для локального внутреннего инструмента достаточно; для публикации в интернете замените на bcrypt).
- Сессии в SQLite, cookie `httponly`, `samesite=lax`.
- Нет CSRF-защиты (локальный внутренний инструмент).

## Деплой на Railway

### 1. Создать проект из GitHub

1. Зайти на https://railway.app → **New Project → Deploy from GitHub repo** → выбрать `YergZakon/validator`.
2. Railway автоматически распознает Python-приложение (через `nixpacks.toml` / `railway.toml`).

### 2. Добавить Volume для SQLite (обязательно!)

Без Volume данные (БД с голосами) пропадут при каждом redeploy.

- В проекте → на сервисе → **Settings → Volumes → + New Volume**
- Mount path: `/app/data`
- Size: 1 GB (более чем достаточно)

### 3. Переменные окружения

В **Variables** добавить:

| Переменная | Значение | Обязательно |
|---|---|---|
| `ADMIN_USERNAME` | например, `admin` | нет (default: `admin`) |
| `ADMIN_PASSWORD` | **сильный пароль** | **да** — иначе будет `admin/admin` |
| `DATA_DIR` | `/app/data` | **да** (чтобы БД легла на Volume) |

### 4. Деплой

Railway сам сбилдит и запустит. При первом старте:
- Инициализируется SQLite-схема
- Из `seed_data/*.json` загрузится 20 статей + 1 956 связей
- Создастся админ с паролем из `ADMIN_PASSWORD`

Проверить: открыть сгенерированный URL (Settings → Domains → Generate Domain).

### 5. Обновления

```bash
git add . && git commit -m "msg" && git push
```

Railway автоматически подхватит коммит и перезапустит. Данные на Volume сохраняются.

---

## Обновление seed-данных (при перегенерации extract_relations_v2.py)

```bash
# 1. Экспортировать текущие голоса:
python3 export.py

# 2. Скопировать новые JSONы в seed_data/:
cp ../mvp_output/*.json seed_data/

# 3. Удалить БД (на Railway — сбросить Volume или подключиться и удалить вручную):
rm data/annotations.db

# 4. При следующем запуске seed произойдёт автоматически.
python3 seed.py
```
