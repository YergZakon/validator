"""import_methodologist_verdicts.py
====================================
Импорт ручных вердиктов методолога из 4 docx-файлов в БД приложения
как голоса виртуального эксперта `methodologist_v1`.

Двухступенчатый pipeline для деплоя на Railway:

1. Локально: парсим docx → пишем `seed_data/methodologist_verdicts.json`.
   docx остаётся вне репозитория, в репо лежит только итоговый JSON.

2. На Railway: команда `import` (или эндпоинт /admin/import-methodologist)
   читает JSON и проставляет голоса. Сопоставляет docx-вердикты с текущими
   relations в БД через prefix-match (docx-тексты усечены до ~80 символов
   и заканчиваются «…»).

Учитываем v18-переименования: СОВЕРШАЕТ→ПРИМЕНЯЕТСЯ_К (с реверсом direction),
ИМЕЕТ_МЕРУ→ИМЕЕТ_ПАРАМЕТР.

Источники docx → article_id:
  - ст. 32 ГК_связи.docx → 1318
  - ст. 18 НК_связи.docx → 2335
  - ст. 77 ТК_связи.docx → 560
  - ст. 55 УК_связи.docx → 639

Формат строки в docx:
    «from_text» → СВЯЗЬ_СТАРАЯ → «to_text» — ВЕРДИКТ

ВЕРДИКТ:
  • ОДОБРИТЬ                  → vote=approve
  • ИЗМЕНИТЬ: УДАЛИТЬ          → vote=reject
  • ИЗМЕНИТЬ: НОВЫЙ_ТИП         → vote=modify, correction_relation_type=НОВЫЙ_ТИП

Запуск:
    cd annotation_app
    python3 import_methodologist_verdicts.py extract       # docx → seed_data/methodologist_verdicts.json
    python3 import_methodologist_verdicts.py import        # JSON → votes (БД)
    python3 import_methodologist_verdicts.py import --dry-run
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path
from collections import Counter, defaultdict

from db import get_conn, init_db
from seed import hash_pwd

ROOT = Path(__file__).parent
DOCX_DIR = ROOT.parent  # /Users/ergalimabiev/Desktop/legalgraph/
VERDICTS_JSON = ROOT / "seed_data" / "methodologist_verdicts.json"

# (docx-файл, article_id из исходного JSON)
DOCX_MAP = [
    ("ст. 32 ГК_связи.docx", 1318),
    ("ст. 18 НК_связи.docx", 2335),
    ("ст. 77 ТК_связи.docx", 560),
    ("ст. 55 УК_связи.docx", 639),
]

# Виртуальный эксперт от чьего лица засчитываем голоса
EXPERT_USERNAME = "methodologist_v1"
EXPERT_FULLNAME = "Методолог (импорт из docx)"

# Старое имя связи (как видел методолог) → возможные имена в v18 после системных правок.
# Если методолог одобрил «ИМЕЕТ_МЕРУ», в v18 эта связь теперь называется ИМЕЕТ_ПАРАМЕТР.
RENAME_MAP = {
    "СОВЕРШАЕТ": {"ПРИМЕНЯЕТСЯ_К"},        # + реверс направления
    "ИМЕЕТ_МЕРУ": {"ИМЕЕТ_ПАРАМЕТР"},
}

# Связи, для которых v18 разворачивает направление: from_text↔to_text
REVERSED_IN_V18 = {"СОВЕРШАЕТ"}

# Паттерн строки docx
RE_LINE = re.compile(
    r'[«"]([^»"]*)[»"]\s*→\s*([А-ЯЁ_]+)\s*→\s*[«"]([^»"]*)[»"]\s*[—–\-]\s*(.+?)$',
    re.UNICODE,
)
RE_VERDICT_MODIFY = re.compile(r'^ИЗМЕНИТЬ\s*:\s*(.+?)$', re.UNICODE)


def parse_verdict(raw: str) -> tuple[str, str | None]:
    """Возвращает (verdict, correction_relation_type|None).
    verdict ∈ {'approve','reject','modify'}."""
    raw = raw.strip()
    if raw.startswith("ОДОБРИТЬ"):
        return "approve", None
    if raw.upper().startswith("ИЗМЕНИТЬ"):
        m = RE_VERDICT_MODIFY.match(raw)
        if not m:
            return "modify", None
        body = m.group(1).strip()
        # Удаление
        if body.upper().startswith("УДАЛИТЬ"):
            return "reject", None
        # Извлекаем тип связи (первое слово, может содержать русские буквы и подчёркивания)
        # Часто в docx после типа идёт пояснение: "ИМЕЕТ_УСЛОВИЕ (условие целиком)"
        # → берём первое слово в верхнем регистре
        m2 = re.match(r'^([А-ЯЁ_]+)', body)
        new_type = m2.group(1) if m2 else body.split()[0]
        return "modify", new_type
    return "modify", None  # неопознанный — на всякий случай modify без коррекции


def parse_docx(path: Path) -> list[dict]:
    """Парсит docx → список вердиктов."""
    from docx import Document  # импортируем тут — модуль не нужен на Railway
    if not path.exists():
        sys.exit(f"✗ Не найден файл {path}")
    doc = Document(str(path))
    verdicts = []
    for p in doc.paragraphs:
        text = p.text.strip()
        if not text:
            continue
        m = RE_LINE.match(text)
        if not m:
            continue
        from_text, old_rel, to_text, raw_verdict = m.groups()
        verdict, correction = parse_verdict(raw_verdict)
        verdicts.append({
            "from_text": from_text.strip(),
            "to_text": to_text.strip(),
            "old_rel": old_rel.strip(),
            "verdict": verdict,
            "correction": correction,
            "raw_verdict": raw_verdict.strip(),
        })
    return verdicts


def cmd_extract(docx_dir: Path) -> None:
    """Stage 1: парсит docx и пишет JSON в seed_data/."""
    bundle = {
        "expert_username": EXPERT_USERNAME,
        "expert_fullname": EXPERT_FULLNAME,
        "articles": [],
    }
    for fname, article_id in DOCX_MAP:
        path = docx_dir / fname
        verdicts = parse_docx(path)
        bundle["articles"].append({
            "article_id": article_id,
            "docx_file": fname,
            "verdicts": verdicts,
        })
        print(f"  ✓ {fname}: {len(verdicts)} вердиктов")

    VERDICTS_JSON.parent.mkdir(parents=True, exist_ok=True)
    VERDICTS_JSON.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    total = sum(len(a["verdicts"]) for a in bundle["articles"])
    print(f"\n→ Записано: {VERDICTS_JSON.name} ({total} вердиктов на 4 статьи)")


def _norm(s: str) -> str:
    """Нормализация текста: collapse whitespace, strip, убрать концевую «…».

    docx-генератор обрезает длинные тексты и добавляет «…» (U+2026) в конце
    обрезанной строки. Эти многоточия мешают prefix-match — снимаем их.
    """
    if not s:
        return ""
    s = s.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    s = re.sub(r"\s+", " ", s).strip()
    # Снимаем хвостовое многоточие/обрыв
    while s and s[-1] in "…...,;:":
        s = s[:-1].rstrip()
    return s


def _prefix_match(short: str, long: str) -> bool:
    """short — текст из docx (возможно усечённый до ≤80 символов с «…»).
    long — полный текст из БД. Считаем совпадением, если после нормализации
    одна строка начинается на другую (или они равны)."""
    if not short or not long:
        return False
    short = _norm(short)
    long = _norm(long)
    if not short or not long:
        return False
    if short == long:
        return True
    # docx обрезан и потерял хвост — БД содержит полный вариант
    return long.startswith(short) or short.startswith(long)


# Кэш relations по article_db_id, чтобы не дёргать SQL на каждом вердикте
_REL_CACHE: dict[int, list] = {}


def _load_relations(cur, article_db_id: int) -> list[dict]:
    if article_db_id in _REL_CACHE:
        return _REL_CACHE[article_db_id]
    cur.execute("""
        SELECT id, rel_id, from_text, to_text, relation_type
          FROM relations
         WHERE article_db_id = ?
    """, (article_db_id,))
    rows = [dict(r) for r in cur.fetchall()]
    _REL_CACHE[article_db_id] = rows
    return rows


def find_relation(cur, article_db_id: int, v: dict) -> tuple[int | None, str]:
    """Подбирает relation.id в БД для одного вердикта.
    Возвращает (relation_id|None, причина_или_тип_совпадения).

    Учитываем:
    1. docx-тексты усечены (~75 символов) → используем prefix-match.
    2. v18 переименовал СОВЕРШАЕТ→ПРИМЕНЯЕТСЯ_К с реверсом и ИМЕЕТ_МЕРУ→ИМЕЕТ_ПАРАМЕТР.
    3. Несколько relations могут иметь одинаковые from_text/to_text → выбираем
       того, чей relation_type ближе к ожидаемому."""
    old_rel = v["old_rel"]
    ft = v["from_text"]
    tt = v["to_text"]
    is_reversed = old_rel in REVERSED_IN_V18
    expected_v18 = RENAME_MAP.get(old_rel, {old_rel})

    rels = _load_relations(cur, article_db_id)

    # Кандидаты в исходном направлении
    direct = [r for r in rels
              if _prefix_match(ft, r["from_text"]) and _prefix_match(tt, r["to_text"])]

    # Кандидаты в реверсе
    reversed_cands = []
    if is_reversed:
        reversed_cands = [r for r in rels
                          if _prefix_match(ft, r["to_text"])
                          and _prefix_match(tt, r["from_text"])]

    # Приоритет 1: совпадение типа в исходном направлении
    for r in direct:
        if r["relation_type"] in expected_v18:
            return r["id"], "exact-direction-and-type"

    # Приоритет 2: совпадение типа в реверсе
    for r in reversed_cands:
        if r["relation_type"] in expected_v18:
            return r["id"], "reversed-direction-and-type"

    # Приоритет 3: исходное направление, любой тип
    if direct:
        return direct[0]["id"], f"exact-direction-but-type-{direct[0]['relation_type']}"

    # Приоритет 4: реверс, любой тип
    if reversed_cands:
        return reversed_cands[0]["id"], f"reversed-direction-but-type-{reversed_cands[0]['relation_type']}"

    return None, "no-match"


def cmd_import(dry_run: bool = False) -> dict:
    """Stage 2: читает JSON и применяет вердикты как голоса в БД.
    Используется и из CLI, и из админ-эндпоинта.

    Возвращает dict-сводку: {applied, unmatched, total, per_article: [...]}
    """
    if not VERDICTS_JSON.exists():
        sys.exit(f"✗ Не найден {VERDICTS_JSON}. Сначала запустите 'extract'.")

    bundle = json.loads(VERDICTS_JSON.read_text(encoding="utf-8"))

    init_db()
    conn = get_conn()
    cur = conn.cursor()

    # Виртуальный эксперт
    cur.execute("SELECT id FROM experts WHERE username = ?", (EXPERT_USERNAME,))
    row = cur.fetchone()
    if row:
        expert_id = row["id"]
    else:
        cur.execute("""
            INSERT INTO experts (username, full_name, password_hash, is_admin)
            VALUES (?, ?, ?, 0)
        """, (EXPERT_USERNAME, bundle.get("expert_fullname", EXPERT_FULLNAME),
              hash_pwd("methodologist_imported")))
        expert_id = cur.lastrowid

    summary = {"applied": 0, "unmatched": 0, "total": 0,
               "expert_id": expert_id, "per_article": []}

    for art_block in bundle["articles"]:
        article_id = art_block["article_id"]
        verdicts = art_block["verdicts"]

        cur.execute(
            "SELECT id, code, article_number FROM articles WHERE article_id = ?",
            (article_id,),
        )
        a = cur.fetchone()
        if not a:
            print(f"⚠ article_id={article_id} не найден — пропускаю")
            continue
        adb = a["id"]

        stats = Counter()
        applied = 0
        unmatched = []
        for v in verdicts:
            rid, why = find_relation(cur, adb, v)
            stats[why] += 1
            if rid is None:
                unmatched.append(v)
                continue
            if dry_run:
                applied += 1
                continue
            cur.execute("""
                INSERT INTO votes
                    (expert_id, relation_id, verdict, comment,
                     correction_relation_type)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(expert_id, relation_id) DO UPDATE SET
                    verdict = excluded.verdict,
                    comment = excluded.comment,
                    correction_relation_type = excluded.correction_relation_type,
                    updated_at = CURRENT_TIMESTAMP
            """, (expert_id, rid, v["verdict"],
                  f"docx: {v['raw_verdict']}", v["correction"]))
            applied += 1

        # Помечаем статью как «выполненную» этим экспертом, чтобы в админке
        # отображалась как валидированная
        if not dry_run and applied > 0:
            cur.execute("""
                INSERT INTO progress (expert_id, article_db_id, completed, completed_at)
                VALUES (?, ?, 1, CURRENT_TIMESTAMP)
                ON CONFLICT(expert_id, article_db_id) DO UPDATE SET
                    completed = 1, completed_at = CURRENT_TIMESTAMP
            """, (expert_id, adb))

        per_art = {
            "code": a["code"], "article_number": a["article_number"],
            "article_id": article_id, "total": len(verdicts),
            "applied": applied, "unmatched": len(unmatched),
            "stats": dict(stats),
        }
        summary["per_article"].append(per_art)
        summary["applied"] += applied
        summary["unmatched"] += len(unmatched)
        summary["total"] += len(verdicts)

        print(f"=== {a['code']} ст.{a['article_number']} (id={article_id}) ===")
        print(f"  применено: {applied}/{len(verdicts)}, не сопоставлено: {len(unmatched)}")
        for why, n in stats.most_common(6):
            print(f"    {n:>4}× {why}")

    if not dry_run:
        conn.commit()
    conn.close()

    print(f"\n=== ИТОГО ===")
    print(f"  вердиктов в JSON:       {summary['total']}")
    print(f"  применено как голос:    {summary['applied']}")
    print(f"  не сопоставлено:        {summary['unmatched']}")
    if dry_run:
        print("  (DRY-RUN — БД не изменена)")
    else:
        print(f"  ✓ записано под expert id={expert_id} ({EXPERT_USERNAME})")
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    p_ex = sub.add_parser("extract", help="docx → seed_data/methodologist_verdicts.json")
    p_ex.add_argument("--docx-dir", default=str(DOCX_DIR),
                      help=f"Папка с docx (default: {DOCX_DIR})")

    p_im = sub.add_parser("import", help="JSON → votes (БД)")
    p_im.add_argument("--dry-run", action="store_true",
                      help="Только посчитать, не писать в БД")

    args = p.parse_args()

    if args.cmd == "extract":
        cmd_extract(Path(args.docx_dir))
    elif args.cmd == "import":
        cmd_import(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
