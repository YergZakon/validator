"""Загружает 20 статей из seed_data/ (или mvp_output/ в dev) в SQLite + создаёт админа."""
import json
import os
import sys
from pathlib import Path
from hashlib import sha256

from db import init_db, get_conn

# В production (Railway) — seed_data/ лежит внутри репозитория.
# В dev — разрешаем также ../mvp_output/
_here = Path(__file__).parent
_candidates = [_here / "seed_data", _here.parent / "mvp_output"]
MVP_DIR = next((p for p in _candidates if p.exists() and any(p.glob("*.json"))),
               _candidates[0])


def hash_pwd(pwd: str) -> str:
    # sha256 + статическая соль, чтобы не зависеть от bcrypt-либ
    return sha256(("legalgraph_salt__" + pwd).encode("utf-8")).hexdigest()


def seed_articles():
    conn = get_conn()
    cur = conn.cursor()
    files = sorted(MVP_DIR.glob("*.json"))
    if not files:
        print(f"ERROR: не найдено JSON-файлов в {MVP_DIR}. "
              f"Сначала запустите extract_relations_v2.py")
        sys.exit(1)

    n_art = n_ent = n_rel = 0
    for fp in files:
        data = json.loads(fp.read_text(encoding="utf-8"))
        cur.execute("""
            INSERT OR IGNORE INTO articles
                (article_id, inner_id, code, article_number, title, text)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            data["article_id"], data.get("inner_id"),
            data["code"], data["article_number"],
            data.get("title", ""), data["text"]
        ))
        cur.execute("SELECT id FROM articles WHERE article_id = ?",
                    (data["article_id"],))
        adb = cur.fetchone()[0]

        # Очистка существующих и вставка
        cur.execute("DELETE FROM entities WHERE article_db_id = ?", (adb,))
        cur.execute("DELETE FROM relations WHERE article_db_id = ?", (adb,))

        for e in data.get("entities", []):
            cur.execute("""
                INSERT INTO entities
                    (article_db_id, entity_id, start, end, text, group_name, label)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (adb, e["id"], e["start"], e["end"], e["text"],
                  e["group"], e["label"]))
            n_ent += 1

        for r in data.get("relations", []):
            cur.execute("""
                INSERT INTO relations
                    (article_db_id, rel_id, from_entity_id, to_entity_id,
                     from_text, to_text, from_label, to_label,
                     relation_type, confidence, trigger_phrase, algorithm, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (adb, r["id"], r["from_id"], r["to_id"],
                  r.get("from_text"), r.get("to_text"),
                  r.get("from_label"), r.get("to_label"),
                  r["relation"], r.get("confidence"),
                  r.get("trigger_phrase"), r.get("algorithm"),
                  r.get("source", "auto")))
            n_rel += 1
        n_art += 1

    conn.commit()
    conn.close()
    print(f"✓ Загружено: {n_art} статей, {n_ent} сущностей, {n_rel} связей")


def seed_admin():
    """Создаёт администратора. Пароль берётся из ADMIN_PASSWORD env, иначе 'admin'."""
    admin_password = os.environ.get("ADMIN_PASSWORD", "admin")
    admin_username = os.environ.get("ADMIN_USERNAME", "admin")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM experts WHERE is_admin = 1")
    if cur.fetchone()[0] == 0:
        cur.execute("""
            INSERT INTO experts (username, full_name, password_hash, is_admin)
            VALUES (?, ?, ?, 1)
        """, (admin_username, "Administrator", hash_pwd(admin_password)))
        conn.commit()
        if admin_password == "admin":
            print(f"✓ Создан {admin_username} / admin — СМЕНИТЕ ПАРОЛЬ!")
        else:
            print(f"✓ Создан {admin_username} (пароль из ADMIN_PASSWORD)")
    conn.close()


if __name__ == "__main__":
    init_db()
    seed_articles()
    seed_admin()
    print("Готово.")
