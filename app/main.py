"""InstaSaved – tự động tải bài đã lưu (Saved) trên Instagram, phân loại và giữ link gốc."""
import csv
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import instaloader
import requests
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from PIL import Image
from pydantic import BaseModel

# ----------------------------------------------------------------- cấu hình
DATA = Path(os.getenv("DATA_DIR", "/data"))
MEDIA = DATA / "media"
THUMBS = DATA / "thumbs"
SESSION_FILE = DATA / "ig_session"
DB_PATH = DATA / "instasaved.db"
STATIC = Path(__file__).parent / "static"
APP_USER = os.getenv("APP_USER", "admin")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")

for _p in (MEDIA, THUMBS):
    _p.mkdir(parents=True, exist_ok=True)

DEFAULTS = {
    "auto_sync": "1",
    "interval_hours": "6",
    "delay_seconds": "4",
    "stop_after_known": "20",
    "use_collections": "1",
    "default_category": "Chưa phân loại",
    "ig_username": "",
    "last_sync_ts": "0",
}
RULE_KINDS = {"owner", "hashtag", "keyword", "collection", "type"}

LOG: deque = deque(maxlen=800)


def log(msg: str):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    LOG.append(line)
    print(line, flush=True)


# ----------------------------------------------------------------- database
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_db_lock = threading.RLock()


def q(sql, args=(), one=False):
    with _db_lock:
        rows = [dict(r) for r in _conn.execute(sql, args).fetchall()]
    return (rows[0] if rows else None) if one else rows


def x(sql, args=()):
    with _db_lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur


def init_db():
    with _db_lock:
        _conn.executescript("""
        CREATE TABLE IF NOT EXISTS posts(
            shortcode TEXT PRIMARY KEY,
            url TEXT, owner TEXT, caption TEXT, hashtags TEXT,
            media_type TEXT, taken_at TEXT, collection TEXT,
            category TEXT, manual INTEGER DEFAULT 0,
            files TEXT, info_file TEXT, sort_key REAL, added_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_posts_cat ON posts(category);
        CREATE INDEX IF NOT EXISTS idx_posts_sort ON posts(sort_key);
        CREATE TABLE IF NOT EXISTS rules(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT, value TEXT, category TEXT, priority INTEGER DEFAULT 100);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        """)
        _conn.commit()


def get_setting(key):
    r = q("SELECT value FROM settings WHERE key=?", (key,), one=True)
    return r["value"] if r else DEFAULTS.get(key, "")


def set_setting(key, value):
    x("INSERT INTO settings(key,value) VALUES(?,?) "
      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


# ----------------------------------------------------------------- tiện ích
def safe_name(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", (s or "").strip()).strip(" .")
    return s[:80] or "_"


def http_get(url, **kw):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=90, **kw)
    r.raise_for_status()
    return r


def download(url: str, dest: Path):
    tmp = dest.with_suffix(dest.suffix + ".part")
    with http_get(url, stream=True) as r, open(tmp, "wb") as f:
        for chunk in r.iter_content(1 << 16):
            f.write(chunk)
    tmp.replace(dest)


def make_thumb(url: str, dest: Path):
    try:
        img = Image.open(io.BytesIO(http_get(url).content)).convert("RGB")
        img.thumbnail((480, 480))
        img.save(dest, "JPEG", quality=82)
    except Exception as e:  # thumbnail lỗi thì vẫn giữ bài
        log(f"  không tạo được ảnh thu nhỏ: {e}")


def classify(row: dict, rules: list) -> str:
    caption = (row.get("caption") or "").lower()
    tags = set((row.get("hashtags") or "").lower().split())
    owner = (row.get("owner") or "").lower()
    coll = (row.get("collection") or "").lower()
    for r in rules:
        v = (r["value"] or "").strip().lower()
        k = r["kind"]
        if not v:
            continue
        if ((k == "owner" and owner == v.lstrip("@"))
                or (k == "hashtag" and v.lstrip("#") in tags)
                or (k == "keyword" and v in caption)
                or (k == "collection" and coll == v)
                or (k == "type" and row.get("media_type") == v)):
            return r["category"]
    if row.get("collection"):
        return row["collection"]
    return get_setting("default_category") or "Chưa phân loại"


def load_rules():
    return q("SELECT * FROM rules ORDER BY priority ASC, id ASC")


def move_post(row: dict, new_cat: str):
    """Chuyển file của một bài sang thư mục danh mục mới."""
    folder = MEDIA / safe_name(new_cat)
    folder.mkdir(parents=True, exist_ok=True)
    old_files = json.loads(row["files"] or "[]")
    new_files = []
    old_dirs = set()
    for rel in old_files + ([row["info_file"]] if row.get("info_file") else []):
        src = MEDIA / rel
        dst = folder / Path(rel).name
        old_dirs.add(src.parent)
        if src.exists() and src.resolve() != dst.resolve():
            shutil.move(str(src), str(dst))
        new_rel = f"{folder.name}/{dst.name}"
        if rel != row.get("info_file"):
            new_files.append(new_rel)
    info = f"{folder.name}/{Path(row['info_file']).name}" if row.get("info_file") else None
    x("UPDATE posts SET category=?, files=?, info_file=? WHERE shortcode=?",
      (new_cat, json.dumps(new_files, ensure_ascii=False), info, row["shortcode"]))
    for d in old_dirs:
        try:
            if d != MEDIA and d.exists() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass


def reclassify_all() -> int:
    rules = load_rules()
    changed = 0
    for row in q("SELECT * FROM posts WHERE manual=0"):
        cat = classify(row, rules)
        if cat != row["category"]:
            move_post(row, cat)
            changed += 1
    return changed


# ----------------------------------------------------------------- instagram
_loader: Optional[instaloader.Instaloader] = None
_pending_2fa: dict = {}


def new_loader():
    return instaloader.Instaloader(
        quiet=True, download_pictures=False, download_videos=False,
        download_video_thumbnails=False, download_geotags=False,
        download_comments=False, save_metadata=False, compress_json=False,
        max_connection_attempts=3, user_agent=UA)


def get_loader():
    global _loader
    if _loader:
        return _loader
    user = get_setting("ig_username")
    if not user or not SESSION_FILE.exists():
        raise RuntimeError("Chưa đăng nhập Instagram (vào Cài đặt để đăng nhập).")
    L = new_loader()
    L.load_session_from_file(user, str(SESSION_FILE))
    _loader = L
    return L


def finish_login(L):
    global _loader
    user = L.test_login()
    if not user:
        raise HTTPException(400, "Instagram không chấp nhận phiên đăng nhập này.")
    L.context.username = user
    L.save_session_to_file(str(SESSION_FILE))
    set_setting("ig_username", user)
    _loader = L
    log(f"Đã đăng nhập Instagram: @{user}")
    return {"ok": True, "username": user}


def parse_cookies(text: str) -> dict:
    text = text.strip()
    if text.startswith("{") or text.startswith("["):
        data = json.loads(text)
        if isinstance(data, list):  # định dạng export của extension (Cookie-Editor…)
            return {c["name"]: c["value"] for c in data if "name" in c}
        return {k: str(v) for k, v in data.items()}
    if text.lower().startswith("cookie:"):
        text = text[7:]
    out = {}
    for part in text.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    if not out and text:  # chỉ dán mỗi giá trị sessionid
        out["sessionid"] = text
    return out


def fetch_collections(L, full: bool) -> dict:
    """Trả về {shortcode: tên bộ sưu tập}. Dùng API nội bộ của Instagram, lỗi thì bỏ qua."""
    result = {}
    try:
        data = L.context.get_iphone_json("api/v1/collections/list/",
                                         {"collection_types": '["MEDIA"]'})
        cols = [c for c in data.get("items", [])
                if c.get("collection_type") != "ALL_MEDIA_AUTO_COLLECTION"]
        log(f"Tìm thấy {len(cols)} bộ sưu tập")
        for c in cols:
            cid, name = c.get("collection_id"), c.get("collection_name") or "Bộ sưu tập"
            max_id, pages = None, 0
            while True:
                params = {"max_id": max_id} if max_id else {}
                d = L.context.get_iphone_json(f"api/v1/feed/collection/{cid}/posts/", params)
                codes = [it["media"]["code"] for it in d.get("items", [])
                         if it.get("media", {}).get("code")]
                for code in codes:
                    result.setdefault(code, name)
                pages += 1
                all_known = codes and all(
                    q("SELECT 1 FROM posts WHERE shortcode=? AND collection=?",
                      (cd, name), one=True) for cd in codes)
                if not d.get("more_available") or (all_known and not full):
                    break
                max_id = d.get("next_max_id")
                time.sleep(2)
    except Exception as e:
        log(f"Không đọc được bộ sưu tập (bỏ qua, vẫn tải bình thường): {e}")
    return result


STATE = {"running": False, "stop": False, "progress": 0, "last_run": None, "last_result": ""}
_sync_lock = threading.Lock()


def save_post(post, collection, rules, sort_key):
    code = post.shortcode
    owner = post.owner_username
    tn = post.typename
    mtype = "album" if tn == "GraphSidecar" else ("video" if post.is_video else "image")
    items = []
    if tn == "GraphSidecar":
        for n in post.get_sidecar_nodes():
            items.append((n.video_url if n.is_video else n.display_url, n.is_video))
    else:
        items.append((post.video_url if post.is_video else post.url, post.is_video))

    row = {
        "shortcode": code,
        "url": f"https://www.instagram.com/p/{code}/",
        "owner": owner,
        "caption": post.caption or "",
        "hashtags": " ".join(t.lower() for t in post.caption_hashtags),
        "media_type": mtype,
        "taken_at": post.date_utc.strftime("%Y-%m-%d %H:%M"),
        "collection": collection,
    }
    cat = classify(row, rules)
    folder = MEDIA / safe_name(cat)
    folder.mkdir(parents=True, exist_ok=True)
    base = f"{safe_name(owner)}_{code}"
    files = []
    for i, (u, isv) in enumerate(items, 1):
        if not u:
            continue
        name = f"{base}_{i}" if len(items) > 1 else base
        name += ".mp4" if isv else ".jpg"
        download(u, folder / name)
        files.append(f"{folder.name}/{name}")

    info = folder / f"{base}.txt"
    info.write_text(
        f"Link: {row['url']}\nTác giả: @{owner}\nNgày đăng: {row['taken_at']} UTC\n"
        f"Bộ sưu tập: {collection or '-'}\nDanh mục: {cat}\n\n{row['caption']}\n",
        encoding="utf-8")
    make_thumb(post.url, THUMBS / f"{code}.jpg")

    x("""INSERT OR REPLACE INTO posts(shortcode,url,owner,caption,hashtags,media_type,
         taken_at,collection,category,manual,files,info_file,sort_key,added_at)
         VALUES(?,?,?,?,?,?,?,?,?,0,?,?,?,?)""",
      (code, row["url"], owner, row["caption"], row["hashtags"], mtype, row["taken_at"],
       collection, cat, json.dumps(files, ensure_ascii=False), f"{folder.name}/{info.name}",
       sort_key, datetime.now().isoformat(timespec="seconds")))
    log(f"  ✓ {code} (@{owner}) → {cat} [{len(files)} file]")


def _sync(full: bool):
    L = get_loader()
    delay = float(get_setting("delay_seconds") or 4)
    stop_after = int(get_setting("stop_after_known") or 20)
    log(f"Bắt đầu đồng bộ {'TOÀN BỘ' if full else 'bài mới'} cho @{L.context.username}")

    colmap = fetch_collections(L, full) if get_setting("use_collections") == "1" else {}
    for code, name in colmap.items():
        x("UPDATE posts SET collection=? WHERE shortcode=? AND IFNULL(collection,'')<>?",
          (name, code, name))

    rules = load_rules()
    profile = instaloader.Profile.own_profile(L.context)
    base_key = time.time() * 1_000_000
    idx = known = new = 0
    for post in profile.get_saved_posts():
        if STATE["stop"]:
            log("Đã dừng theo yêu cầu.")
            break
        idx += 1
        if q("SELECT 1 FROM posts WHERE shortcode=?", (post.shortcode,), one=True):
            known += 1
            if not full and known >= stop_after:
                log(f"Gặp {known} bài đã có liên tiếp → dừng (đồng bộ nhanh).")
                break
            continue
        known = 0
        try:
            save_post(post, colmap.get(post.shortcode), rules, base_key - idx)
            new += 1
            STATE["progress"] = new
        except Exception as e:
            log(f"  ✗ bỏ qua {post.shortcode}: {e}")
        time.sleep(delay)

    moved = reclassify_all()
    msg = f"Xong: {new} bài mới, {moved} bài đổi danh mục."
    log(msg)
    STATE["last_result"] = msg


def start_sync(full=False) -> bool:
    if not _sync_lock.acquire(blocking=False):
        return False
    STATE.update(running=True, stop=False, progress=0)
    set_setting("last_sync_ts", time.time())

    def work():
        global _loader
        try:
            _sync(full)
        except instaloader.exceptions.LoginRequiredException as e:
            _loader = None
            STATE["last_result"] = "Phiên đăng nhập hết hạn – hãy đăng nhập lại."
            log(f"Lỗi: {e}")
        except Exception as e:
            STATE["last_result"] = f"Lỗi: {e}"
            log(f"Lỗi đồng bộ: {e}")
        finally:
            STATE["running"] = False
            STATE["last_run"] = datetime.now().isoformat(timespec="seconds")
            _sync_lock.release()

    threading.Thread(target=work, daemon=True).start()
    return True


def scheduler():
    while True:
        time.sleep(60)
        try:
            if get_setting("auto_sync") != "1" or not SESSION_FILE.exists():
                continue
            interval = float(get_setting("interval_hours") or 6) * 3600
            if time.time() - float(get_setting("last_sync_ts") or 0) >= interval:
                start_sync(False)
        except Exception as e:
            log(f"Lỗi lịch tự động: {e}")


# ----------------------------------------------------------------- web app
@asynccontextmanager
async def lifespan(_app):
    init_db()
    threading.Thread(target=scheduler, daemon=True).start()
    log("InstaSaved đã khởi động.")
    yield


security = HTTPBasic(auto_error=False)


def auth(creds: Optional[HTTPBasicCredentials] = Depends(security)):
    if not APP_PASSWORD:
        return
    ok = creds and secrets.compare_digest(creds.username.encode(), APP_USER.encode()) \
        and secrets.compare_digest(creds.password.encode(), APP_PASSWORD.encode())
    if not ok:
        raise HTTPException(401, "Sai mật khẩu", headers={"WWW-Authenticate": 'Basic realm="InstaSaved"'})


app = FastAPI(title="InstaSaved", lifespan=lifespan, dependencies=[Depends(auth)])


def post_out(r: dict) -> dict:
    r = dict(r)
    r["files"] = json.loads(r["files"] or "[]")
    r["thumb"] = f"/thumbs/{r['shortcode']}.jpg" if (THUMBS / f"{r['shortcode']}.jpg").exists() else None
    return r


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status():
    return {
        **STATE,
        "logged_in": SESSION_FILE.exists() and bool(get_setting("ig_username")),
        "username": get_setting("ig_username"),
        "total": q("SELECT COUNT(*) c FROM posts", one=True)["c"],
    }


@app.post("/api/sync")
def sync(full: bool = False):
    if not start_sync(full):
        raise HTTPException(409, "Đang đồng bộ rồi.")
    return {"ok": True}


@app.post("/api/sync/stop")
def sync_stop():
    STATE["stop"] = True
    return {"ok": True}


@app.get("/api/posts")
def posts(category: str = "", owner: str = "", media_type: str = "",
          offset: int = 0, limit: int = 60, search: str = ""):
    term = search.strip()
    where, args = [], []
    if category:
        where.append("category=?"); args.append(category)
    if owner:
        where.append("owner=?"); args.append(owner)
    if media_type:
        where.append("media_type=?"); args.append(media_type)
    if term:
        where.append("(caption LIKE ? OR owner LIKE ? OR hashtags LIKE ? OR shortcode LIKE ?)")
        args += [f"%{term}%"] * 4
    sql = "FROM posts" + (" WHERE " + " AND ".join(where) if where else "")
    total = q(f"SELECT COUNT(*) c {sql}", args, one=True)["c"]
    rows = q(f"SELECT * {sql} ORDER BY sort_key DESC LIMIT ? OFFSET ?",
             args + [min(limit, 200), offset])
    return {"total": total, "items": [post_out(r) for r in rows]}


@app.get("/api/categories")
def categories():
    return q("SELECT category name, COUNT(*) count FROM posts GROUP BY category ORDER BY count DESC")


class CategoryIn(BaseModel):
    category: str


@app.patch("/api/posts/{code}")
def set_category(code: str, body: CategoryIn):
    row = q("SELECT * FROM posts WHERE shortcode=?", (code,), one=True)
    if not row:
        raise HTTPException(404)
    cat = body.category.strip()
    if not cat:  # bỏ trống = trả về phân loại tự động
        x("UPDATE posts SET manual=0 WHERE shortcode=?", (code,))
        row["manual"] = 0
        cat = classify(row, load_rules())
    else:
        x("UPDATE posts SET manual=1 WHERE shortcode=?", (code,))
    move_post(row, cat)
    return post_out(q("SELECT * FROM posts WHERE shortcode=?", (code,), one=True))


@app.delete("/api/posts/{code}")
def delete_post(code: str):
    row = q("SELECT * FROM posts WHERE shortcode=?", (code,), one=True)
    if not row:
        raise HTTPException(404)
    for rel in json.loads(row["files"] or "[]") + [row["info_file"]]:
        if rel and (MEDIA / rel).exists():
            (MEDIA / rel).unlink()
    (THUMBS / f"{code}.jpg").unlink(missing_ok=True)
    x("DELETE FROM posts WHERE shortcode=?", (code,))
    return {"ok": True}


class RuleIn(BaseModel):
    kind: str
    value: str
    category: str
    priority: int = 100


@app.get("/api/rules")
def rules_list():
    return load_rules()


@app.post("/api/rules")
def rules_add(r: RuleIn):
    if r.kind not in RULE_KINDS or not r.value.strip() or not r.category.strip():
        raise HTTPException(400, "Quy tắc không hợp lệ")
    x("INSERT INTO rules(kind,value,category,priority) VALUES(?,?,?,?)",
      (r.kind, r.value.strip(), r.category.strip(), r.priority))
    return load_rules()


@app.delete("/api/rules/{rid}")
def rules_del(rid: int):
    x("DELETE FROM rules WHERE id=?", (rid,))
    return load_rules()


@app.post("/api/reclassify")
def reclassify():
    n = reclassify_all()
    log(f"Phân loại lại: {n} bài đổi danh mục.")
    return {"changed": n}


@app.get("/api/settings")
def settings_get():
    return {k: get_setting(k) for k in DEFAULTS if k != "last_sync_ts"}


@app.post("/api/settings")
def settings_set(body: dict):
    for k, v in body.items():
        if k in DEFAULTS and k not in ("ig_username", "last_sync_ts"):
            set_setting(k, v)
    return settings_get()


class CookieLogin(BaseModel):
    cookies: str


@app.post("/api/login/cookies")
def login_cookies(body: CookieLogin):
    try:
        c = parse_cookies(body.cookies)
    except Exception:
        raise HTTPException(400, "Không đọc được cookie.")
    if not c.get("sessionid"):
        raise HTTPException(400, "Thiếu cookie sessionid.")
    if not c.get("csrftoken"):
        try:
            r = requests.get("https://www.instagram.com/", cookies=c,
                             headers={"User-Agent": UA}, timeout=30)
            c["csrftoken"] = r.cookies.get("csrftoken") or secrets.token_hex(16)
        except Exception:
            c["csrftoken"] = secrets.token_hex(16)
    L = new_loader()
    L.context.load_session("", c)
    return finish_login(L)


class PasswordLogin(BaseModel):
    username: str
    password: str


@app.post("/api/login/password")
def login_password(body: PasswordLogin):
    L = new_loader()
    try:
        L.login(body.username.strip(), body.password)
    except instaloader.exceptions.TwoFactorAuthRequiredException:
        _pending_2fa["L"] = L
        return {"need_2fa": True}
    except instaloader.exceptions.BadCredentialsException:
        raise HTTPException(400, "Sai tên đăng nhập hoặc mật khẩu.")
    except Exception as e:
        raise HTTPException(400, f"Instagram chặn đăng nhập ({e}). Hãy dùng cách dán cookie.")
    return finish_login(L)


class TwoFA(BaseModel):
    code: str


@app.post("/api/login/2fa")
def login_2fa(body: TwoFA):
    L = _pending_2fa.pop("L", None)
    if not L:
        raise HTTPException(400, "Không có phiên chờ mã 2FA.")
    try:
        L.two_factor_login(body.code.strip())
    except Exception as e:
        raise HTTPException(400, f"Mã 2FA không đúng: {e}")
    return finish_login(L)


@app.post("/api/logout")
def logout():
    global _loader
    _loader = None
    SESSION_FILE.unlink(missing_ok=True)
    set_setting("ig_username", "")
    log("Đã đăng xuất Instagram.")
    return {"ok": True}


@app.get("/api/logs")
def logs():
    return list(LOG)


@app.get("/api/export.csv")
def export_csv(category: str = ""):
    rows = q("SELECT * FROM posts" + (" WHERE category=?" if category else "")
             + " ORDER BY sort_key DESC", (category,) if category else ())
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["link", "tac_gia", "danh_muc", "bo_suu_tap", "loai", "ngay_dang",
                "hashtag", "file", "chu_thich"])
    for r in rows:
        w.writerow([r["url"], r["owner"], r["category"], r["collection"] or "",
                    r["media_type"], r["taken_at"], r["hashtags"],
                    " | ".join(json.loads(r["files"] or "[]")), r["caption"]])
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="instagram_saved.csv"'})


def _safe_file(root: Path, rel: str) -> Path:
    p = (root / rel).resolve()
    if not str(p).startswith(str(root.resolve()) + os.sep) or not p.is_file():
        raise HTTPException(404)
    return p


@app.get("/media/{path:path}")
def media(path: str):
    return FileResponse(_safe_file(MEDIA, path))


@app.get("/thumbs/{name}")
def thumbs(name: str):
    return FileResponse(_safe_file(THUMBS, name))
