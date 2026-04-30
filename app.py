"""FastAPI-приложение экспертной валидации связей.

Запуск:
    cd annotation_app
    python3 -m pip install -r requirements.txt  (единожды)
    python3 seed.py                             (единожды: загружает статьи и админа)
    uvicorn app:app --reload --port 8080
    Открыть: http://localhost:8080
"""
from __future__ import annotations
import os
import secrets
import uuid
import io
import csv as csv_mod
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from db import get_conn, init_db
from seed import hash_pwd, seed_articles, seed_admin
from iaa import compute_all
from export import export_all, EXPORT_DIR

APP_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

app = FastAPI(title="LegalGraph Expert Validation")
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


def _bootstrap():
    """Автоинициализация: схема + статьи + admin при первом старте (в т.ч. на Railway)."""
    init_db()
    conn = get_conn()
    has_articles = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] > 0
    has_admin = conn.execute("SELECT COUNT(*) FROM experts WHERE is_admin=1").fetchone()[0] > 0
    conn.close()
    if not has_articles:
        try:
            seed_articles()
        except SystemExit:
            print("⚠ seed_data отсутствует, пропускаю загрузку статей.")
    if not has_admin:
        seed_admin()


_bootstrap()


@app.get("/healthz")
def healthz():
    return {"ok": True}

# ---------------------------------------------------------------------------
# Аутентификация
# ---------------------------------------------------------------------------


def current_expert(request: Request) -> Optional[dict]:
    sid = request.cookies.get("sid")
    if not sid:
        return None
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT e.* FROM sessions s JOIN experts e ON e.id = s.expert_id
        WHERE s.sid = ?
    """, (sid,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def require_login(request: Request) -> dict:
    exp = current_expert(request)
    if not exp:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return exp


def require_admin(request: Request) -> dict:
    exp = require_login(request)
    if not exp["is_admin"]:
        raise HTTPException(status_code=403, detail="Требуются права администратора")
    return exp


# ---------------------------------------------------------------------------
# Логин / логаут
# ---------------------------------------------------------------------------


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if current_expert(request):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM experts WHERE username = ?", (username,))
    u = cur.fetchone()
    if not u or u["password_hash"] != hash_pwd(password):
        conn.close()
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "Неверный логин или пароль"}
        )
    sid = secrets.token_urlsafe(32)
    cur.execute("INSERT INTO sessions (sid, expert_id) VALUES (?, ?)", (sid, u["id"]))
    conn.commit()
    conn.close()
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie("sid", sid, httponly=True, samesite="lax")
    return resp


@app.get("/logout")
def logout(request: Request):
    sid = request.cookies.get("sid")
    if sid:
        conn = get_conn()
        conn.execute("DELETE FROM sessions WHERE sid = ?", (sid,))
        conn.commit()
        conn.close()
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie("sid")
    return resp


# ---------------------------------------------------------------------------
# Dashboard / список статей
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    exp = current_expert(request)
    if not exp:
        return RedirectResponse(url="/login", status_code=303)

    conn = get_conn()
    cur = conn.cursor()

    # Список статей + прогресс текущего эксперта
    cur.execute("""
        SELECT a.*,
            (SELECT COUNT(*) FROM relations WHERE article_db_id = a.id) AS n_rel,
            (SELECT COUNT(*) FROM votes v
              JOIN relations r ON r.id = v.relation_id
              WHERE r.article_db_id = a.id AND v.expert_id = ?) AS n_voted,
            (SELECT completed FROM progress
              WHERE article_db_id = a.id AND expert_id = ?) AS completed
        FROM articles a
        ORDER BY a.code, a.article_number
    """, (exp["id"], exp["id"]))
    articles = [dict(r) for r in cur.fetchall()]

    # Глобальная статистика
    cur.execute("SELECT COUNT(*) FROM relations")
    total_rel = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM votes WHERE expert_id = ?", (exp["id"],))
    my_votes = cur.fetchone()[0]
    cur.execute("""
        SELECT COUNT(DISTINCT relation_id) FROM votes
    """)
    any_voted = cur.fetchone()[0]
    conn.close()

    return templates.TemplateResponse("dashboard.html", {
        "request": request, "expert": exp, "articles": articles,
        "total_rel": total_rel, "my_votes": my_votes, "any_voted": any_voted,
    })


# ---------------------------------------------------------------------------
# Annotate
# ---------------------------------------------------------------------------


@app.get("/annotate/{article_db_id}", response_class=HTMLResponse)
def annotate_page(request: Request, article_db_id: int, filter: str = "all"):
    exp = current_expert(request)
    if not exp:
        return RedirectResponse(url="/login", status_code=303)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM articles WHERE id = ?", (article_db_id,))
    art = cur.fetchone()
    if not art:
        raise HTTPException(404, "Статья не найдена")
    art = dict(art)

    # Фильтр
    where_filter = ""
    if filter == "unrated":
        where_filter = """AND NOT EXISTS (SELECT 1 FROM votes
                          WHERE relation_id = r.id AND expert_id = ?)"""
    elif filter in ("approve", "reject", "modify"):
        where_filter = "AND EXISTS (SELECT 1 FROM votes WHERE relation_id = r.id AND expert_id = ? AND verdict = ?)"

    # Связи статьи + вердикт текущего эксперта
    params = [exp["id"], article_db_id]
    if filter == "unrated":
        params.append(exp["id"])
    elif filter in ("approve", "reject", "modify"):
        params.extend([exp["id"], filter])

    cur.execute(f"""
        SELECT r.*,
            (SELECT verdict FROM votes WHERE relation_id = r.id AND expert_id = ?) AS my_verdict,
            (SELECT comment FROM votes WHERE relation_id = r.id AND expert_id = ?) AS my_comment,
            (SELECT correction_relation_type FROM votes WHERE relation_id = r.id AND expert_id = ?) AS my_correction,
            (SELECT COUNT(*) FROM votes WHERE relation_id = r.id) AS n_all_votes,
            (SELECT COUNT(*) FROM votes WHERE relation_id = r.id AND verdict='approve') AS n_approve_all,
            (SELECT COUNT(*) FROM votes WHERE relation_id = r.id AND verdict='reject')  AS n_reject_all,
            (SELECT COUNT(*) FROM votes WHERE relation_id = r.id AND verdict='modify')  AS n_modify_all
        FROM relations r
        WHERE r.article_db_id = ?
          {where_filter}
        ORDER BY r.relation_type, r.id
    """, (exp["id"], exp["id"], exp["id"]) + tuple(params[1:]))
    relations = [dict(r) for r in cur.fetchall()]

    # Сущности для подсветки
    cur.execute("SELECT * FROM entities WHERE article_db_id = ? ORDER BY start",
                (article_db_id,))
    entities = [dict(e) for e in cur.fetchall()]

    # Прогресс по статье
    cur.execute("""
        SELECT COUNT(*) AS total,
               (SELECT COUNT(*) FROM votes v JOIN relations r ON r.id = v.relation_id
                WHERE r.article_db_id = ? AND v.expert_id = ?) AS voted
        FROM relations WHERE article_db_id = ?
    """, (article_db_id, exp["id"], article_db_id))
    progress = dict(cur.fetchone())

    # Навигация: предыдущая / следующая статья
    cur.execute("SELECT id FROM articles WHERE id < ? ORDER BY id DESC LIMIT 1",
                (article_db_id,))
    row = cur.fetchone()
    prev_id = row["id"] if row else None
    cur.execute("SELECT id FROM articles WHERE id > ? ORDER BY id ASC LIMIT 1",
                (article_db_id,))
    row = cur.fetchone()
    next_id = row["id"] if row else None

    # Типы связей для модификации
    RELATION_TYPES = [
        "СОВЕРШАЕТ", "НАПРАВЛЕНО_НА", "УСТАНАВЛИВАЕТ_МОДАЛЬНОСТЬ",
        "ИМЕЕТ_УСЛОВИЕ", "ИСКЛЮЧАЕТ", "ИМЕЕТ_МЕРУ",
        "ВЛЕЧЁТ", "ОПРЕДЕЛЯЕТ", "СОСТОИТ_ИЗ", "ССЫЛАЕТСЯ_НА",
        "ИЕРАРХИЯ", "ДЕЙСТВУЕТ_В",
    ]
    conn.close()

    return templates.TemplateResponse("annotate.html", {
        "request": request, "expert": exp, "article": art,
        "relations": relations, "entities": entities,
        "progress": progress, "prev_id": prev_id, "next_id": next_id,
        "relation_types": RELATION_TYPES, "filter": filter,
    })


@app.post("/api/vote")
def api_vote(request: Request,
             relation_id: int = Form(...),
             verdict: str = Form(...),
             comment: Optional[str] = Form(None),
             correction_relation_type: Optional[str] = Form(None),
             correction_from_id: Optional[str] = Form(None),
             correction_to_id: Optional[str] = Form(None)):
    exp = current_expert(request)
    if not exp:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if verdict not in ("approve", "reject", "modify"):
        return JSONResponse({"error": "bad verdict"}, status_code=400)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO votes
            (expert_id, relation_id, verdict, comment,
             correction_relation_type, correction_from_id, correction_to_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(expert_id, relation_id) DO UPDATE SET
            verdict = excluded.verdict,
            comment = excluded.comment,
            correction_relation_type = excluded.correction_relation_type,
            correction_from_id = excluded.correction_from_id,
            correction_to_id = excluded.correction_to_id,
            updated_at = CURRENT_TIMESTAMP
    """, (exp["id"], relation_id, verdict, comment,
          correction_relation_type, correction_from_id, correction_to_id))
    # Подтягиваем обновлённые счётчики
    cur.execute("""
        SELECT COUNT(*) AS n_all,
               SUM(CASE WHEN verdict='approve' THEN 1 ELSE 0 END) AS n_approve,
               SUM(CASE WHEN verdict='reject'  THEN 1 ELSE 0 END) AS n_reject,
               SUM(CASE WHEN verdict='modify'  THEN 1 ELSE 0 END) AS n_modify
        FROM votes WHERE relation_id = ?
    """, (relation_id,))
    counts = dict(cur.fetchone())
    conn.commit()
    conn.close()
    return {"ok": True, "counts": counts}


@app.post("/api/add-relation")
def api_add_relation(request: Request,
                     article_db_id: int = Form(...),
                     from_entity_id: str = Form(...),
                     to_entity_id: str = Form(...),
                     relation_type: str = Form(...),
                     comment: Optional[str] = Form(None)):
    exp = current_expert(request)
    if not exp:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    conn = get_conn()
    conn.execute("""
        INSERT INTO expert_additions
            (expert_id, article_db_id, from_entity_id, to_entity_id,
             relation_type, comment)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (exp["id"], article_db_id, from_entity_id, to_entity_id,
          relation_type, comment))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/complete-article")
def api_complete(request: Request, article_db_id: int = Form(...)):
    exp = current_expert(request)
    if not exp:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    conn = get_conn()
    conn.execute("""
        INSERT INTO progress (expert_id, article_db_id, completed, completed_at)
        VALUES (?, ?, 1, CURRENT_TIMESTAMP)
        ON CONFLICT(expert_id, article_db_id) DO UPDATE SET
            completed = 1, completed_at = CURRENT_TIMESTAMP
    """, (exp["id"], article_db_id))
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    exp = current_expert(request)
    if not exp or not exp["is_admin"]:
        return RedirectResponse(url="/login", status_code=303)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT e.id, e.username, e.full_name, e.email, e.is_admin, e.created_at,
               (SELECT COUNT(*) FROM votes WHERE expert_id = e.id) AS n_votes,
               (SELECT COUNT(*) FROM expert_additions WHERE expert_id = e.id) AS n_add,
               (SELECT COUNT(*) FROM progress WHERE expert_id = e.id AND completed = 1) AS n_done
        FROM experts e ORDER BY e.id
    """)
    experts = [dict(r) for r in cur.fetchall()]

    # Глобальная статистика
    cur.execute("SELECT COUNT(*) FROM articles")
    n_articles = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM relations")
    n_relations = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM votes")
    n_votes = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT relation_id) FROM votes")
    n_rel_voted = cur.fetchone()[0]

    # Статистика по типам связей
    cur.execute("""
        SELECT relation_type,
               COUNT(*) AS total,
               (SELECT COUNT(DISTINCT v.relation_id)
                FROM votes v JOIN relations r2 ON r2.id = v.relation_id
                WHERE r2.relation_type = r.relation_type) AS n_voted,
               (SELECT SUM(CASE WHEN v.verdict='approve' THEN 1 ELSE 0 END)
                FROM votes v JOIN relations r2 ON r2.id = v.relation_id
                WHERE r2.relation_type = r.relation_type) AS n_approve,
               (SELECT SUM(CASE WHEN v.verdict='reject' THEN 1 ELSE 0 END)
                FROM votes v JOIN relations r2 ON r2.id = v.relation_id
                WHERE r2.relation_type = r.relation_type) AS n_reject,
               (SELECT SUM(CASE WHEN v.verdict='modify' THEN 1 ELSE 0 END)
                FROM votes v JOIN relations r2 ON r2.id = v.relation_id
                WHERE r2.relation_type = r.relation_type) AS n_modify
        FROM relations r
        GROUP BY relation_type
        ORDER BY total DESC
    """)
    by_rel = [dict(r) for r in cur.fetchall()]

    iaa = compute_all(conn)
    conn.close()
    return templates.TemplateResponse("admin.html", {
        "request": request, "expert": exp, "experts": experts,
        "n_articles": n_articles, "n_relations": n_relations,
        "n_votes": n_votes, "n_rel_voted": n_rel_voted,
        "by_rel": by_rel, "iaa": iaa,
    })


@app.post("/admin/experts/add")
def admin_add_expert(request: Request,
                     username: str = Form(...),
                     full_name: str = Form(""),
                     email: str = Form(""),
                     password: str = Form(...),
                     is_admin: Optional[str] = Form(None)):
    admin = current_expert(request)
    if not admin or not admin["is_admin"]:
        return RedirectResponse(url="/login", status_code=303)
    conn = get_conn()
    try:
        conn.execute("""
            INSERT INTO experts (username, full_name, email, password_hash, is_admin)
            VALUES (?, ?, ?, ?, ?)
        """, (username, full_name, email, hash_pwd(password),
              1 if is_admin == "on" else 0))
        conn.commit()
    except Exception as e:
        conn.close()
        raise HTTPException(400, f"Ошибка: {e}")
    conn.close()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/experts/{eid}/delete")
def admin_delete_expert(request: Request, eid: int):
    admin = current_expert(request)
    if not admin or not admin["is_admin"]:
        return RedirectResponse(url="/login", status_code=303)
    if eid == admin["id"]:
        raise HTTPException(400, "Нельзя удалить себя")
    conn = get_conn()
    conn.execute("DELETE FROM votes WHERE expert_id = ?", (eid,))
    conn.execute("DELETE FROM expert_additions WHERE expert_id = ?", (eid,))
    conn.execute("DELETE FROM progress WHERE expert_id = ?", (eid,))
    conn.execute("DELETE FROM sessions WHERE expert_id = ?", (eid,))
    conn.execute("DELETE FROM experts WHERE id = ?", (eid,))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/experts/{eid}/reset-password")
def admin_reset_pwd(request: Request, eid: int, new_password: str = Form(...)):
    admin = current_expert(request)
    if not admin or not admin["is_admin"]:
        return RedirectResponse(url="/login", status_code=303)
    conn = get_conn()
    conn.execute("UPDATE experts SET password_hash = ? WHERE id = ?",
                 (hash_pwd(new_password), eid))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/export")
def admin_export(request: Request):
    admin = current_expert(request)
    if not admin or not admin["is_admin"]:
        return RedirectResponse(url="/login", status_code=303)
    export_all()
    return RedirectResponse(url="/admin?exported=1", status_code=303)


@app.post("/admin/reseed")
def admin_reseed(request: Request):
    """Перетягивает articles/entities/relations из seed_data/*.json
    через UPSERT — голоса экспертов сохраняются благодаря стабильным rel_id.
    Используется для миграции при обновлении схемы связей."""
    admin = current_expert(request)
    if not admin or not admin["is_admin"]:
        return RedirectResponse(url="/login", status_code=303)
    try:
        seed_articles()
        return RedirectResponse(url="/admin?reseeded=1", status_code=303)
    except Exception as e:
        raise HTTPException(500, f"seed failed: {e}")


@app.get("/admin/download/{filename}")
def admin_download(request: Request, filename: str):
    admin = current_expert(request)
    if not admin or not admin["is_admin"]:
        return RedirectResponse(url="/login", status_code=303)
    safe = (EXPORT_DIR / filename).resolve()
    if not str(safe).startswith(str(EXPORT_DIR.resolve())):
        raise HTTPException(400, "bad path")
    if not safe.exists():
        raise HTTPException(404, "файл не найден — сначала сгенерируйте экспорт")
    return FileResponse(safe, filename=filename)


# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
