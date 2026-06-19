# -*- coding: utf-8 -*-
import os
import sqlite3
from datetime import datetime, timedelta

# Где хранить базу. На Render/VPS укажи DATA_DIR на ПОСТОЯННЫЙ диск (например /var/data),
# иначе при перезапуске файловая система очищается и все данные пропадают.
DATA_DIR = os.getenv("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    DATA_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH") or os.path.join(DATA_DIR, "marketer_db.db")


def _conn():
    # WAL + busy_timeout: устойчивость к "database is locked" при параллельных
    # userbot-ах и веб-API одновременно. timeout=30 даёт время дождаться лока.
    c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA foreign_keys=ON")
    except Exception:
        pass
    return c


def init_db():
    c = _conn()
    cur = c.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users(
        telegram_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
        first_seen TEXT, last_seen TEXT, trial_used INTEGER DEFAULT 0)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS sessions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, phone TEXT,
        api_id INTEGER, api_hash TEXT, session_string TEXT,
        status TEXT DEFAULT 'active', created TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS subscriptions(
        user_id INTEGER PRIMARY KEY, expires_at TEXT, source TEXT,
        notified_expired INTEGER DEFAULT 0)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS paid_chats(
        user_id INTEGER, chat_id TEXT, PRIMARY KEY(user_id, chat_id))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, ts TEXT, text TEXT)""")
    # Индексы под частые запросы (логи, сессии, подписки).
    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_logs_user ON logs(user_id, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_sessions_owner ON sessions(owner_id)",
        "CREATE INDEX IF NOT EXISTS idx_subs_exp ON subscriptions(expires_at)",
    ):
        try:
            cur.execute(stmt)
        except Exception:
            pass
    # Миграция: колонка proxy для сессий (прокси на каждый аккаунт).
    try:
        cur.execute("ALTER TABLE sessions ADD COLUMN proxy TEXT")
    except Exception:
        pass
    c.commit()
    c.close()


# ===== USERS =====
def track_user(tg_id, username=None, first_name=None):
    c = _conn()
    cur = c.cursor()
    now = datetime.now().isoformat(timespec="seconds")
    row = cur.execute("SELECT telegram_id FROM users WHERE telegram_id=?", (tg_id,)).fetchone()
    if row:
        cur.execute("UPDATE users SET username=?, first_name=?, last_seen=? WHERE telegram_id=?",
                    (username, first_name, now, tg_id))
    else:
        cur.execute("INSERT INTO users(telegram_id,username,first_name,first_seen,last_seen,trial_used) "
                    "VALUES(?,?,?,?,?,0)", (tg_id, username, first_name, now, now))
    c.commit()
    c.close()


def get_all_users():
    c = _conn()
    rows = c.execute("SELECT * FROM users ORDER BY first_seen DESC").fetchall()
    c.close()
    return [dict(r) for r in rows]


def count_users():
    c = _conn()
    n = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    c.close()
    return n


def trial_used(user_id):
    c = _conn()
    row = c.execute("SELECT trial_used FROM users WHERE telegram_id=?", (user_id,)).fetchone()
    c.close()
    return bool(row and row["trial_used"])


# ===== SESSIONS =====
def add_session(owner_id, phone, api_id, api_hash, ss, proxy=None):
    c = _conn()
    now = datetime.now().isoformat(timespec="seconds")
    c.execute("INSERT INTO sessions(owner_id,phone,api_id,api_hash,session_string,status,created,proxy) "
              "VALUES(?,?,?,?,?,?,?,?)", (owner_id, phone, api_id, api_hash, ss, "active", now, proxy))
    c.commit()
    c.close()


def get_sessions(owner_id):
    c = _conn()
    rows = c.execute("SELECT * FROM sessions WHERE owner_id=? ORDER BY id", (owner_id,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def get_all_sessions():
    c = _conn()
    rows = c.execute("SELECT * FROM sessions ORDER BY id").fetchall()
    c.close()
    return [dict(r) for r in rows]


def delete_session(session_id, owner_id=None):
    c = _conn()
    if owner_id is None:
        c.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    else:
        c.execute("DELETE FROM sessions WHERE id=? AND owner_id=?", (session_id, owner_id))
    c.commit()
    c.close()


def delete_all_sessions(owner_id):
    c = _conn()
    c.execute("DELETE FROM sessions WHERE owner_id=?", (owner_id,))
    c.commit()
    c.close()


# ===== SUBSCRIPTIONS =====
def subscription_expiry(user_id):
    c = _conn()
    row = c.execute("SELECT expires_at FROM subscriptions WHERE user_id=?", (user_id,)).fetchone()
    c.close()
    if not row or not row["expires_at"]:
        return None
    try:
        return datetime.fromisoformat(row["expires_at"])
    except Exception:
        return None


def set_subscription(user_id, days, source="paid"):
    base = datetime.now()
    cur_exp = subscription_expiry(user_id)
    if cur_exp and cur_exp > base:
        base = cur_exp
    exp = base + timedelta(days=days)
    c = _conn()
    c.execute("""INSERT INTO subscriptions(user_id,expires_at,source,notified_expired)
                 VALUES(?,?,?,0)
                 ON CONFLICT(user_id) DO UPDATE SET
                   expires_at=excluded.expires_at, source=excluded.source, notified_expired=0""",
              (user_id, exp.isoformat(timespec="seconds"), source))
    c.commit()
    c.close()
    return exp


def grant_trial(user_id, days):
    """Выдаёт бесплатный период ОДИН раз. Возвращает дату окончания или None."""
    c = _conn()
    cur = c.cursor()
    row = cur.execute("SELECT trial_used FROM users WHERE telegram_id=?", (user_id,)).fetchone()
    if row and row["trial_used"]:
        c.close()
        return None
    cur.execute("UPDATE users SET trial_used=1 WHERE telegram_id=?", (user_id,))
    c.commit()
    c.close()
    return set_subscription(user_id, days, source="trial")


def is_subscribed(user_id):
    exp = subscription_expiry(user_id)
    return bool(exp and exp > datetime.now())


def get_expired_to_notify():
    now = datetime.now().isoformat(timespec="seconds")
    c = _conn()
    rows = c.execute("SELECT user_id FROM subscriptions WHERE expires_at < ? AND notified_expired=0",
                     (now,)).fetchall()
    c.close()
    return [r["user_id"] for r in rows]


def mark_notified(user_id):
    c = _conn()
    c.execute("UPDATE subscriptions SET notified_expired=1 WHERE user_id=?", (user_id,))
    c.commit()
    c.close()


def count_active_subs():
    now = datetime.now().isoformat(timespec="seconds")
    c = _conn()
    n = c.execute("SELECT COUNT(*) AS n FROM subscriptions WHERE expires_at > ?", (now,)).fetchone()["n"]
    c.close()
    return n


# ===== PAID CHATS =====
def is_paid_chat(user_id, chat_id):
    c = _conn()
    row = c.execute("SELECT 1 FROM paid_chats WHERE user_id=? AND chat_id=?",
                    (user_id, str(chat_id))).fetchone()
    c.close()
    return bool(row)


def mark_paid_chat(user_id, chat_id):
    c = _conn()
    c.execute("INSERT OR IGNORE INTO paid_chats(user_id,chat_id) VALUES(?,?)",
              (user_id, str(chat_id)))
    c.commit()
    c.close()


# ===== LOGS =====
def add_log(user_id, text):
    c = _conn()
    ts = datetime.now().strftime("%H:%M:%S")
    c.execute("INSERT INTO logs(user_id,ts,text) VALUES(?,?,?)", (user_id, ts, text))
    c.commit()
    c.close()


def get_logs(user_id, limit=25):
    c = _conn()
    rows = c.execute("SELECT ts,text FROM logs WHERE user_id=? ORDER BY id DESC LIMIT ?",
                     (user_id, limit)).fetchall()
    c.close()
    return [{"ts": r["ts"], "text": r["text"]} for r in rows]


# ===== РЕФЕРАЛЫ / ШАБЛОНЫ / РАСШИРЕННЫЕ ПОДПИСКИ (доп. слой) =====
PAID_SOURCES = ("paid", "stars", "manual", "admin", "card", "ton", "ua", "crypto")


def _ensure_extra_tables():
    c = _conn()
    cur = c.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS referrals(
        referrer_id INTEGER, referred_id INTEGER PRIMARY KEY, ts TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS templates(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, name TEXT,
        payload TEXT, created TEXT)""")
    c.commit()
    c.close()


_ensure_extra_tables()


def subscription_source(user_id):
    c = _conn()
    row = c.execute("SELECT source FROM subscriptions WHERE user_id=?", (user_id,)).fetchone()
    c.close()
    return (row["source"] if row else None)


def is_paid_plan(user_id):
    """True, если у пользователя активна именно ПЛАТНАЯ подписка (не триал)."""
    if not is_subscribed(user_id):
        return False
    src = subscription_source(user_id)
    return (src in PAID_SOURCES) if src else False


def add_subscription_hours(user_id, hours, source="referral"):
    """Продлевает подписку на N часов (для рефералов и бонусов)."""
    c = _conn()
    cur = c.cursor()
    row = cur.execute("SELECT expires_at FROM subscriptions WHERE user_id=?", (user_id,)).fetchone()
    now = datetime.now()
    base = now
    if row and row["expires_at"]:
        try:
            cur_exp = datetime.fromisoformat(row["expires_at"])
            if cur_exp > now:
                base = cur_exp
        except Exception:
            base = now
    new_exp = (base + timedelta(hours=hours)).isoformat(timespec="seconds")
    if row:
        cur.execute("UPDATE subscriptions SET expires_at=?, notified_expired=0 WHERE user_id=?",
                    (new_exp, user_id))
    else:
        cur.execute("INSERT INTO subscriptions(user_id,expires_at,source,notified_expired) VALUES(?,?,?,0)",
                    (user_id, new_exp, source))
    c.commit()
    c.close()
    return new_exp


def remove_subscription(user_id):
    c = _conn()
    c.execute("DELETE FROM subscriptions WHERE user_id=?", (user_id,))
    c.commit()
    c.close()


def get_user_by_username(username):
    if not username:
        return None
    u = username.lstrip("@").strip().lower()
    c = _conn()
    row = c.execute("SELECT * FROM users WHERE lower(username)=?", (u,)).fetchone()
    c.close()
    return dict(row) if row else None


def add_referral(referrer_id, referred_id):
    """Фиксирует приглашение. True — если это новый реферал."""
    if not referrer_id or not referred_id or int(referrer_id) == int(referred_id):
        return False
    c = _conn()
    cur = c.cursor()
    exists = cur.execute("SELECT 1 FROM referrals WHERE referred_id=?", (referred_id,)).fetchone()
    if exists:
        c.close()
        return False
    cur.execute("INSERT INTO referrals(referrer_id,referred_id,ts) VALUES(?,?,?)",
                (referrer_id, referred_id, datetime.now().isoformat(timespec="seconds")))
    c.commit()
    c.close()
    return True


def count_referrals(user_id):
    c = _conn()
    row = c.execute("SELECT COUNT(*) AS n FROM referrals WHERE referrer_id=?", (user_id,)).fetchone()
    c.close()
    return row["n"] if row else 0


def add_template(user_id, name, payload):
    c = _conn()
    cur = c.cursor()
    cur.execute("INSERT INTO templates(user_id,name,payload,created) VALUES(?,?,?,?)",
                (user_id, name, payload, datetime.now().isoformat(timespec="seconds")))
    tid = cur.lastrowid
    c.commit()
    c.close()
    return tid


def get_templates(user_id):
    c = _conn()
    rows = c.execute("SELECT * FROM templates WHERE user_id=? ORDER BY id DESC", (user_id,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def get_template(template_id, user_id):
    c = _conn()
    row = c.execute("SELECT * FROM templates WHERE id=? AND user_id=?", (template_id, user_id)).fetchone()
    c.close()
    return dict(row) if row else None


def delete_template(template_id, user_id):
    c = _conn()
    c.execute("DELETE FROM templates WHERE id=? AND user_id=?", (template_id, user_id))
    c.commit()
    c.close()


# ===== АДМИН: БАН / ЛОГ ДЕЙСТВИЙ / ОПЛАТЫ (доп. слой) =====
def _ensure_admin_tables():
    c = _conn()
    cur = c.cursor()
    try:
        cur.execute("ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0")
    except Exception:
        pass
    cur.execute("""CREATE TABLE IF NOT EXISTS admin_actions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER, action TEXT,
        target_id INTEGER, detail TEXT, ts TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL,
        currency TEXT, source TEXT, ts TEXT)""")
    c.commit()
    c.close()


_ensure_admin_tables()


def is_banned(user_id):
    c = _conn()
    try:
        row = c.execute("SELECT banned FROM users WHERE telegram_id=?", (user_id,)).fetchone()
    except Exception:
        row = None
    c.close()
    return bool(row and row["banned"])


def ban_user(user_id):
    c = _conn()
    c.execute("UPDATE users SET banned=1 WHERE telegram_id=?", (user_id,))
    c.commit()
    c.close()


def unban_user(user_id):
    c = _conn()
    c.execute("UPDATE users SET banned=0 WHERE telegram_id=?", (user_id,))
    c.commit()
    c.close()


def add_admin_action(admin_id, action, target_id=None, detail=""):
    c = _conn()
    c.execute("INSERT INTO admin_actions(admin_id,action,target_id,detail,ts) VALUES(?,?,?,?,?)",
              (admin_id, action, target_id, detail, datetime.now().isoformat(timespec="seconds")))
    c.commit()
    c.close()


def get_admin_actions(limit=30):
    c = _conn()
    rows = c.execute("SELECT * FROM admin_actions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def log_payment(user_id, amount, currency="XTR", source="stars"):
    c = _conn()
    c.execute("INSERT INTO payments(user_id,amount,currency,source,ts) VALUES(?,?,?,?,?)",
              (user_id, amount, currency, source, datetime.now().isoformat(timespec="seconds")))
    c.commit()
    c.close()


def revenue_stars(days=None):
    """Сумма звёзд за период (или за всё время, если days=None)."""
    c = _conn()
    if days:
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        row = c.execute("SELECT COALESCE(SUM(amount),0) AS s FROM payments WHERE currency='XTR' AND ts>=?",
                        (since,)).fetchone()
    else:
        row = c.execute("SELECT COALESCE(SUM(amount),0) AS s FROM payments WHERE currency='XTR'").fetchone()
    c.close()
    return int(row["s"] or 0)


def count_paid_subs():
    """Активные ПЛАТНЫЕ подписки (без триала)."""
    now = datetime.now().isoformat(timespec="seconds")
    qmarks = ",".join("?" for _ in PAID_SOURCES)
    c = _conn()
    row = c.execute("SELECT COUNT(*) AS n FROM subscriptions WHERE expires_at>? AND source IN (%s)" % qmarks,
                    (now, *PAID_SOURCES)).fetchone()
    c.close()
    return row["n"] if row else 0


# ===================== ДНЕВНОЙ ЛИМИТ ЦИКЛОВ (бесплатный тариф) =====================
# Персистим рассылки, счётчики и напоминания — чтобы переживать рестарт хостинга.
def _ensure_runtime_tables():
    c = _conn()
    cur = c.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS daily_usage(
        user_id INTEGER, day TEXT, cycles INTEGER DEFAULT 0,
        PRIMARY KEY(user_id, day))""")
    # Состояние рассылок для возобновления после рестарта.
    cur.execute("""CREATE TABLE IF NOT EXISTS jobs(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, name TEXT,
        config TEXT, status TEXT, progress TEXT, created TEXT, updated TEXT)""")
    # Отметки о воронке напоминаний об окончании триала (чтобы не спамить).
    cur.execute("""CREATE TABLE IF NOT EXISTS reminders(
        user_id INTEGER, kind TEXT, ts TEXT, PRIMARY KEY(user_id, kind))""")
    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_daily_user ON daily_usage(user_id, day)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)",
        "CREATE INDEX IF NOT EXISTS idx_ref_referrer ON referrals(referrer_id)",
    ):
        try:
            cur.execute(stmt)
        except Exception:
            pass
    c.commit()
    c.close()


_ensure_runtime_tables()


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def get_daily_cycles(user_id):
    """Сколько циклов уже истрачено сегодня."""
    c = _conn()
    row = c.execute("SELECT cycles FROM daily_usage WHERE user_id=? AND day=?",
                    (user_id, _today())).fetchone()
    c.close()
    return int(row["cycles"]) if row else 0


def add_daily_cycles(user_id, n):
    """Добавляет n циклов к сегодняшнему расходу. Возвращает новое значение."""
    c = _conn()
    c.execute("""INSERT INTO daily_usage(user_id, day, cycles) VALUES(?,?,?)
                 ON CONFLICT(user_id, day) DO UPDATE SET cycles=cycles+excluded.cycles""",
              (user_id, _today(), int(n)))
    row = c.execute("SELECT cycles FROM daily_usage WHERE user_id=? AND day=?",
                    (user_id, _today())).fetchone()
    c.commit()
    c.close()
    return int(row["cycles"]) if row else int(n)


def daily_cycles_left(user_id, limit):
    return max(int(limit) - get_daily_cycles(user_id), 0)


# ===================== ПЕРСИСТ РАССЫЛОК (возобновление) =====================
import json as _json


def save_job(user_id, name, config_dict, status="running", progress=None, job_db_id=None):
    """Сохраняет/обновляет состояние рассылки. Возвращает id записи в БД."""
    now = datetime.now().isoformat(timespec="seconds")
    c = _conn()
    cur = c.cursor()
    cfg = _json.dumps(config_dict, ensure_ascii=False)
    prg = _json.dumps(progress or {}, ensure_ascii=False)
    if job_db_id:
        cur.execute("UPDATE jobs SET status=?, progress=?, updated=? WHERE id=?",
                    (status, prg, now, job_db_id))
        c.commit()
        c.close()
        return job_db_id
    cur.execute("INSERT INTO jobs(user_id,name,config,status,progress,created,updated) VALUES(?,?,?,?,?,?,?)",
                (user_id, name, cfg, status, prg, now, now))
    jid = cur.lastrowid
    c.commit()
    c.close()
    return jid


def update_job_status(job_db_id, status, progress=None):
    if not job_db_id:
        return
    now = datetime.now().isoformat(timespec="seconds")
    c = _conn()
    if progress is not None:
        c.execute("UPDATE jobs SET status=?, progress=?, updated=? WHERE id=?",
                  (status, _json.dumps(progress, ensure_ascii=False), now, job_db_id))
    else:
        c.execute("UPDATE jobs SET status=?, updated=? WHERE id=?", (status, now, job_db_id))
    c.commit()
    c.close()


def get_resumable_jobs():
    """Рассылки, которые были активны в момент остановки бота."""
    c = _conn()
    rows = c.execute("SELECT * FROM jobs WHERE status IN ('running','paused') ORDER BY id").fetchall()
    c.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["config"] = _json.loads(d.get("config") or "{}")
        except Exception:
            d["config"] = {}
        try:
            d["progress"] = _json.loads(d.get("progress") or "{}")
        except Exception:
            d["progress"] = {}
        out.append(d)
    return out


# ===================== ВОРОНКА НАПОМИНАНИЙ =====================
def reminder_sent(user_id, kind):
    c = _conn()
    row = c.execute("SELECT 1 FROM reminders WHERE user_id=? AND kind=?", (user_id, kind)).fetchone()
    c.close()
    return bool(row)


def mark_reminder(user_id, kind):
    c = _conn()
    c.execute("INSERT OR REPLACE INTO reminders(user_id,kind,ts) VALUES(?,?,?)",
              (user_id, kind, datetime.now().isoformat(timespec="seconds")))
    c.commit()
    c.close()


def clear_reminders(user_id):
    """После продления подписки сбрасываем отметки, чтобы воронка работала снова."""
    c = _conn()
    c.execute("DELETE FROM reminders WHERE user_id=?", (user_id,))
    c.commit()
    c.close()


def get_active_trials():
    """Юзеры с активным триалом (для напоминаний «осталось N дней»)."""
    now = datetime.now().isoformat(timespec="seconds")
    c = _conn()
    rows = c.execute("SELECT user_id, expires_at FROM subscriptions WHERE source='trial' AND expires_at>?",
                     (now,)).fetchall()
    c.close()
    return [(r["user_id"], r["expires_at"]) for r in rows]


# ===================== ЛИДЕРБОРД РЕФЕРАЛОВ =====================
def referral_leaderboard(limit=10):
    c = _conn()
    rows = c.execute("""SELECT r.referrer_id AS uid, COUNT(*) AS n,
                               u.username AS username, u.first_name AS first_name
                        FROM referrals r LEFT JOIN users u ON u.telegram_id=r.referrer_id
                        GROUP BY r.referrer_id ORDER BY n DESC LIMIT ?""", (limit,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def referral_rank(user_id):
    """Место юзера в рейтинге рефералов (1 = лучший) или None."""
    c = _conn()
    rows = c.execute("""SELECT referrer_id, COUNT(*) AS n FROM referrals
                        GROUP BY referrer_id ORDER BY n DESC""").fetchall()
    c.close()
    for i, r in enumerate(rows, 1):
        if r["referrer_id"] == user_id:
            return i, r["n"]
    return None, 0


def count_templates(user_id):
    c = _conn()
    row = c.execute("SELECT COUNT(*) AS n FROM templates WHERE user_id=?", (user_id,)).fetchone()
    c.close()
    return int(row["n"]) if row else 0
