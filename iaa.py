"""Inter-Annotator Agreement: Cohen's κ (2 experts) и Fleiss' κ (n ≥ 3).
Плюс per-relation и общие сводки."""
from __future__ import annotations
import math
from collections import defaultdict, Counter

CATEGORIES = ("approve", "reject", "modify")


def cohen_kappa(labels_a: list[str], labels_b: list[str]) -> float:
    """κ Коэна для двух аннотаторов."""
    if len(labels_a) != len(labels_b) or not labels_a:
        return 0.0
    n = len(labels_a)
    po = sum(1 for a, b in zip(labels_a, labels_b) if a == b) / n
    cats = set(labels_a) | set(labels_b)
    pe = 0.0
    for c in cats:
        pa = labels_a.count(c) / n
        pb = labels_b.count(c) / n
        pe += pa * pb
    if pe >= 1.0:
        return 1.0 if po >= 1.0 else 0.0
    return (po - pe) / (1 - pe)


def fleiss_kappa(matrix: list[list[int]]) -> float:
    """Fleiss' κ. matrix[i][j] = сколько аннотаторов поставили категорию j на элемент i.
    Количество аннотаторов по каждому элементу может различаться, но для корректности
    мы усредняем по элементам с ≥ 2 аннотаторами."""
    filtered = [row for row in matrix if sum(row) >= 2]
    if len(filtered) < 2:
        return 0.0
    # Каждому элементу должно быть одинаковое n — берём минимум
    n_per_item = [sum(r) for r in filtered]
    n = min(n_per_item)
    if n < 2:
        return 0.0
    N = len(filtered)
    k = len(filtered[0])
    # Обрежем матрицу до n по каждой строке
    normalised = []
    for row in filtered:
        total = sum(row)
        if total != n:
            # пропорционально масштабируем (сохраняя округление) — на практике редко нужно
            row = [int(round(v * n / total)) for v in row]
            # страховка: сумма ≈ n
            diff = n - sum(row)
            if diff != 0:
                row[row.index(max(row))] += diff
        normalised.append(row)
    # Доля раз, когда категория j назначалась
    p_j = [sum(row[j] for row in normalised) / (N * n) for j in range(k)]
    # Согласие по элементу i
    P_i = []
    for row in normalised:
        agreement = (sum(v * v for v in row) - n) / (n * (n - 1)) if n > 1 else 0
        P_i.append(agreement)
    P_mean = sum(P_i) / N
    Pe = sum(p * p for p in p_j)
    if Pe >= 1.0:
        return 1.0 if P_mean >= 1.0 else 0.0
    return (P_mean - Pe) / (1 - Pe)


def kappa_interpretation(k: float) -> str:
    if k < 0:      return "нет согласия"
    if k < 0.20:   return "slight (ничтожное)"
    if k < 0.40:   return "fair (слабое)"
    if k < 0.60:   return "moderate (умеренное)"
    if k < 0.80:   return "substantial (существенное)"
    return "almost perfect (почти полное)"


def compute_all(conn) -> dict:
    """Возвращает словарь со всеми метриками IAA по БД."""
    cur = conn.cursor()
    # Сбор: для каждой relation_id список (expert_id, verdict)
    cur.execute("""
        SELECT r.id, r.relation_type, v.expert_id, v.verdict
        FROM relations r
        JOIN votes v ON v.relation_id = r.id
        ORDER BY r.id, v.expert_id
    """)
    per_rel: dict[int, list[tuple[int, str]]] = defaultdict(list)
    rel_type: dict[int, str] = {}
    for rid, rtype, eid, verdict in cur.fetchall():
        per_rel[rid].append((eid, verdict))
        rel_type[rid] = rtype

    if not per_rel:
        return {"total_voted": 0, "global_kappa": None,
                "pairwise": {}, "per_relation_type": {}}

    # Матрица Fleiss: relation × category
    matrix = []
    matrix_by_rtype: dict[str, list[list[int]]] = defaultdict(list)
    for rid, votes in per_rel.items():
        row = [0, 0, 0]  # approve, reject, modify
        for _, v in votes:
            row[CATEGORIES.index(v)] += 1
        matrix.append(row)
        matrix_by_rtype[rel_type[rid]].append(row)

    global_kappa = fleiss_kappa(matrix)

    # Попарный Cohen's kappa между всеми парами экспертов
    # по относительно полному набору (элементы, оценённые обоими)
    cur.execute("SELECT id, username FROM experts WHERE is_admin = 0 ORDER BY id")
    experts = cur.fetchall()
    pairwise: dict[str, dict] = {}
    for i in range(len(experts)):
        for j in range(i + 1, len(experts)):
            e_i = experts[i]
            e_j = experts[j]
            # собираем пары (verdict_i, verdict_j) по совпадающим relation_id
            cur.execute("""
                SELECT va.verdict, vb.verdict
                FROM votes va
                JOIN votes vb ON va.relation_id = vb.relation_id
                WHERE va.expert_id = ? AND vb.expert_id = ?
            """, (e_i["id"], e_j["id"]))
            pairs = cur.fetchall()
            if len(pairs) < 5:
                continue
            a = [p[0] for p in pairs]
            b = [p[1] for p in pairs]
            k = cohen_kappa(a, b)
            pairwise[f"{e_i['username']} ↔ {e_j['username']}"] = {
                "kappa": round(k, 3),
                "interpretation": kappa_interpretation(k),
                "n_overlapping_items": len(pairs),
            }

    per_rel_type = {}
    for rt, rows in matrix_by_rtype.items():
        if len(rows) >= 2:
            k = fleiss_kappa(rows)
            per_rel_type[rt] = {
                "kappa": round(k, 3),
                "n_relations": len(rows),
                "interpretation": kappa_interpretation(k),
            }

    return {
        "total_voted": len(per_rel),
        "global_kappa": round(global_kappa, 3),
        "global_interpretation": kappa_interpretation(global_kappa),
        "pairwise": pairwise,
        "per_relation_type": per_rel_type,
    }


if __name__ == "__main__":
    from db import get_conn
    conn = get_conn()
    result = compute_all(conn)
    import json
    print(json.dumps(result, ensure_ascii=False, indent=2))
