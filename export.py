"""Экспорт результатов экспертной валидации для машинного обучения.

Выходные форматы:
  exports/ml_dataset.jsonl    — одна запись на связь с gold-меткой и распределением голосов
  exports/ml_dataset.csv      — то же в CSV
  exports/expert_additions.jsonl — ручные связи, добавленные экспертами
  exports/disputed.jsonl      — связи с низким согласием (кандидаты на active learning)
  exports/gold_relations.jsonl — только approved (для обучения классификатора «связь верна»)
  exports/per_expert_stats.csv — статистика по каждому эксперту
"""
from __future__ import annotations
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

from db import get_conn, DATA_DIR

EXPORT_DIR = DATA_DIR / "exports"
EXPORT_DIR.mkdir(parents=True, exist_ok=True)


def majority_verdict(votes: list[str]) -> tuple[str, float]:
    """Возвращает мажоритарный вердикт и уровень согласия [0..1]."""
    if not votes:
        return ("none", 0.0)
    c = Counter(votes)
    top_label, top_n = c.most_common(1)[0]
    return (top_label, top_n / len(votes))


def export_all():
    conn = get_conn()
    cur = conn.cursor()

    # Собираем связи + голоса
    cur.execute("""
        SELECT r.id, r.rel_id, r.from_entity_id, r.to_entity_id,
               r.from_text, r.to_text, r.from_label, r.to_label,
               r.relation_type, r.confidence, r.trigger_phrase, r.algorithm,
               r.source, a.code, a.article_number, a.article_id, a.text AS article_text
        FROM relations r
        JOIN articles a ON a.id = r.article_db_id
        ORDER BY a.code, a.article_number, r.id
    """)
    all_rels = cur.fetchall()

    cur.execute("""
        SELECT v.relation_id, v.expert_id, e.username, v.verdict,
               v.correction_relation_type, v.correction_from_id, v.correction_to_id, v.comment
        FROM votes v JOIN experts e ON e.id = v.expert_id
    """)
    votes_by_rel: dict[int, list[dict]] = defaultdict(list)
    for vr in cur.fetchall():
        votes_by_rel[vr["relation_id"]].append(dict(vr))

    # 1. ml_dataset.jsonl + ml_dataset.csv
    ml_jsonl = EXPORT_DIR / "ml_dataset.jsonl"
    ml_csv = EXPORT_DIR / "ml_dataset.csv"
    gold_jsonl = EXPORT_DIR / "gold_relations.jsonl"
    disputed_jsonl = EXPORT_DIR / "disputed.jsonl"

    with ml_jsonl.open("w", encoding="utf-8") as f_ml, \
         ml_csv.open("w", encoding="utf-8", newline="") as f_csv, \
         gold_jsonl.open("w", encoding="utf-8") as f_gold, \
         disputed_jsonl.open("w", encoding="utf-8") as f_disp:

        writer = csv.writer(f_csv)
        writer.writerow([
            "rel_id", "code", "article_number", "article_id",
            "from_entity_id", "from_text", "from_label",
            "relation_type",
            "to_entity_id", "to_text", "to_label",
            "auto_confidence", "trigger_phrase", "algorithm",
            "n_votes", "n_approve", "n_reject", "n_modify",
            "majority_verdict", "agreement_ratio",
            "gold_label"
        ])

        for r in all_rels:
            votes = votes_by_rel.get(r["id"], [])
            verdicts = [v["verdict"] for v in votes]
            maj, agr = majority_verdict(verdicts)
            n_app = verdicts.count("approve")
            n_rej = verdicts.count("reject")
            n_mod = verdicts.count("modify")
            # gold_label: approve → relation_type, reject → "NONE", modify → majority correction
            gold = None
            if maj == "approve":
                gold = r["relation_type"]
            elif maj == "reject":
                gold = "NONE"
            elif maj == "modify":
                corr = Counter(v["correction_relation_type"] for v in votes
                               if v["verdict"] == "modify" and v["correction_relation_type"])
                gold = corr.most_common(1)[0][0] if corr else None

            rec = {
                "rel_id": r["rel_id"], "code": r["code"],
                "article_number": r["article_number"], "article_id": r["article_id"],
                "article_text": r["article_text"],
                "from_entity_id": r["from_entity_id"], "from_text": r["from_text"],
                "from_label": r["from_label"],
                "to_entity_id": r["to_entity_id"], "to_text": r["to_text"],
                "to_label": r["to_label"],
                "relation_type": r["relation_type"],
                "auto_confidence": r["confidence"],
                "trigger_phrase": r["trigger_phrase"],
                "algorithm": r["algorithm"],
                "source": r["source"],
                "votes": [{
                    "expert": v["username"], "verdict": v["verdict"],
                    "correction_relation_type": v["correction_relation_type"],
                    "comment": v["comment"]
                } for v in votes],
                "n_votes": len(verdicts),
                "n_approve": n_app, "n_reject": n_rej, "n_modify": n_mod,
                "majority_verdict": maj if verdicts else None,
                "agreement_ratio": round(agr, 3),
                "gold_label": gold,
            }
            f_ml.write(json.dumps(rec, ensure_ascii=False) + "\n")
            writer.writerow([
                r["rel_id"], r["code"], r["article_number"], r["article_id"],
                r["from_entity_id"], r["from_text"], r["from_label"],
                r["relation_type"],
                r["to_entity_id"], r["to_text"], r["to_label"],
                r["confidence"], r["trigger_phrase"], r["algorithm"],
                len(verdicts), n_app, n_rej, n_mod,
                maj if verdicts else "", agr, gold or ""
            ])
            # gold — только approved или modify с согласием ≥ 0.67
            if (maj == "approve" and agr >= 0.67) or (maj == "modify" and agr >= 0.67):
                f_gold.write(json.dumps(rec, ensure_ascii=False) + "\n")
            # disputed — низкое согласие при ≥ 2 голосах
            if len(verdicts) >= 2 and agr < 0.67:
                f_disp.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # 2. Экспертные дополнения
    cur.execute("""
        SELECT ea.*, e.username, a.code, a.article_number, a.article_id
        FROM expert_additions ea
        JOIN experts e ON e.id = ea.expert_id
        JOIN articles a ON a.id = ea.article_db_id
    """)
    with (EXPORT_DIR / "expert_additions.jsonl").open("w", encoding="utf-8") as f:
        for ea in cur.fetchall():
            f.write(json.dumps(dict(ea), ensure_ascii=False, default=str) + "\n")

    # 3. Статистика по экспертам
    cur.execute("""
        SELECT e.id, e.username, e.full_name,
               COUNT(DISTINCT v.relation_id) AS total_votes,
               SUM(CASE WHEN v.verdict='approve' THEN 1 ELSE 0 END) AS n_approve,
               SUM(CASE WHEN v.verdict='reject'  THEN 1 ELSE 0 END) AS n_reject,
               SUM(CASE WHEN v.verdict='modify'  THEN 1 ELSE 0 END) AS n_modify,
               COUNT(DISTINCT ea.id) AS additions,
               COUNT(DISTINCT p.article_db_id) AS completed_articles
        FROM experts e
        LEFT JOIN votes v ON v.expert_id = e.id
        LEFT JOIN expert_additions ea ON ea.expert_id = e.id
        LEFT JOIN progress p ON p.expert_id = e.id AND p.completed = 1
        WHERE e.is_admin = 0
        GROUP BY e.id
    """)
    with (EXPORT_DIR / "per_expert_stats.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["expert_id", "username", "full_name",
                         "total_votes", "n_approve", "n_reject", "n_modify",
                         "additions", "completed_articles"])
        for row in cur.fetchall():
            writer.writerow(list(row))

    conn.close()
    print(f"✓ Экспорт сохранён в {EXPORT_DIR}/")
    for p in sorted(EXPORT_DIR.glob("*")):
        print(f"   {p.name}  ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    export_all()
