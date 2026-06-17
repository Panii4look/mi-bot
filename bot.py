#!/usr/bin/env python3
"""
Bot de Telegram — Gestor de Grupos Pro v2
==========================================
Novedades:
  - Zona horaria por grupo (compartir ubicación)
  - Editar anuncios existentes
  - Programar hora de lanzamiento de anuncios
  - Solo grupos pueden ser líder / sub-grupo (no chats privados)
  - Eliminar grupo líder
  - /mainconnect <id>  — establecer líder por ID
  - /subconnect <id>   — registrar sub-grupo por ID
"""

import os, re, json, string, random, logging, sqlite3
from datetime import datetime, timedelta

import pytz

try:
    from timezonefinder import TimezoneFinder
    _TF = TimezoneFinder()
    HAS_TF = True
except ImportError:
    _TF = None
    HAS_TF = False

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

# ─── Configuración ────────────────────────────────────────────────────────────

BOT_TOKEN  = os.getenv("BOT_TOKEN", "8918957692:AAH3LMGWv056fnTpt-BSr7OQDPIdpstDR9s")
ADMIN_ID   = int(os.getenv("ADMIN_ID", "8901320155"))
DB_PATH    = "bot_data.db"
DEFAULT_TZ = "America/Argentina/Buenos_Aires"

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone=DEFAULT_TZ)

# ─── Conversation States ──────────────────────────────────────────────────────

# Crear anuncio (5 pasos)
ANN_TEXT, ANN_MEDIA, ANN_BUTTONS, ANN_INTERVAL, ANN_STARTTIME = range(5)

# Menú: palabras
WORD_ADD_WAIT, WORD_DEL_WAIT = range(5, 7)

# Menú: anuncio / subgrupo
ANN_PREVIEW_WAIT, ANN_DEL_WAIT, SUB_DEL_WAIT = range(7, 10)

# Editar anuncio
ANN_EDIT_ID, ANN_EDIT_FIELD, ANN_EDIT_VALUE = range(10, 13)

# ─── Base de Datos ────────────────────────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS licenses (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                code         TEXT    UNIQUE NOT NULL,
                chat_id      INTEGER,
                chat_name    TEXT,
                days         INTEGER NOT NULL,
                activated_at TEXT,
                expires_at   TEXT,
                is_active    INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS forbidden_words (
                chat_id   INTEGER,
                word      TEXT,
                is_global INTEGER DEFAULT 0,
                PRIMARY KEY (chat_id, word)
            );
            CREATE TABLE IF NOT EXISTS warnings (
                chat_id INTEGER,
                user_id INTEGER,
                count   INTEGER DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS announcements (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id        INTEGER NOT NULL,
                text           TEXT    NOT NULL,
                media_file_id  TEXT,
                media_type     TEXT,
                buttons_json   TEXT    DEFAULT '[]',
                interval_hours REAL    NOT NULL,
                start_time     TEXT,
                created_at     TEXT
            );
            CREATE TABLE IF NOT EXISTS leader_groups (
                chat_id INTEGER PRIMARY KEY,
                set_at  TEXT
            );
            CREATE TABLE IF NOT EXISTS subgroups (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                leader_chat_id INTEGER NOT NULL,
                sub_chat_id    INTEGER UNIQUE NOT NULL,
                sub_name       TEXT
            );
            CREATE TABLE IF NOT EXISTS group_settings (
                chat_id  INTEGER PRIMARY KEY,
                timezone TEXT DEFAULT 'America/Argentina/Buenos_Aires'
            );
        """)
        for sql in [
            "ALTER TABLE forbidden_words ADD COLUMN is_global INTEGER DEFAULT 0",
            "ALTER TABLE announcements ADD COLUMN start_time TEXT",
        ]:
            try:
                conn.execute(sql)
            except Exception:
                pass


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_group_tz(chat_id: int) -> str:
    with get_db() as conn:
        row = conn.execute(
            "SELECT timezone FROM group_settings WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return row["timezone"] if row else DEFAULT_TZ

# ─── Licencias ────────────────────────────────────────────────────────────────

def _gen_code(length=8) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))


async def check_license(chat_id: int) -> tuple[bool, str]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT expires_at FROM licenses WHERE chat_id=? AND is_active=1", (chat_id,)
        ).fetchone()
    if not row:
        return False, "none"
    exp       = datetime.fromisoformat(row["expires_at"])
    grace_end = exp + timedelta(hours=24)
    now       = datetime.now()
    if now > grace_end:
        return False, "expired"
    if now > exp:
        return True, "grace"
    return True, "active"


async def require_license(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if update.effective_chat.type == "private":
        return True
    ok, status = await check_license(update.effective_chat.id)
    if not ok:
        msg = (
            "⛔ Este grupo no tiene licencia activa.\n"
            "Usá <code>/activar &lt;código&gt;</code> para activar el bot."
            if status == "none" else
            "⛔ La licencia de este grupo <b>venció</b>. Contactá al admin para renovarla."
        )
        await update.effective_message.reply_text(msg, parse_mode="HTML")
    elif status == "grace":
        await update.effective_message.reply_text(
            "⚠️ La licencia venció hace menos de 24hs. ¡Renovála pronto!", parse_mode="HTML"
        )
    return ok

# ─── Admin del bot ────────────────────────────────────────────────────────────

def is_bot_admin(update: Update) -> bool:
    return update.effective_user.id == ADMIN_ID


async def cmd_generar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_admin(update):
        return
    try:
        dias = int(context.args[0]) if context.args else 30
    except ValueError:
        dias = 30
    code = _gen_code()
    with get_db() as conn:
        conn.execute("INSERT INTO licenses (code, days) VALUES (?, ?)", (code, dias))
    await update.message.reply_text(
        f"🔑 <b>Código generado:</b>\n\n<code>{code}</code>\n\n"
        f"⏱ Válido por <b>{dias} días</b> desde la activación.", parse_mode="HTML"
    )


async def cmd_revocar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Uso: /revocar <chat_id>")
        return
    try:
        chat_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("El chat_id debe ser un número.")
        return
    with get_db() as conn:
        conn.execute("UPDATE licenses SET is_active=0 WHERE chat_id=?", (chat_id,))
    try:
        await context.bot.send_message(
            chat_id, "⛔ La licencia fue <b>revocada</b>.", parse_mode="HTML"
        )
    except Exception:
        pass
    await update.message.reply_text(
        f"✅ Licencia revocada para <code>{chat_id}</code>.", parse_mode="HTML"
    )


async def cmd_licencias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_admin(update):
        return
    with get_db() as conn:
        rows = conn.execute(
            "SELECT code, chat_id, chat_name, days, expires_at, is_active "
            "FROM licenses ORDER BY id DESC LIMIT 25"
        ).fetchall()
    if not rows:
        await update.message.reply_text("No hay licencias generadas.")
        return
    now = datetime.now()
    lineas = []
    for r in rows:
        if not r["chat_id"]:
            estado = "⏳ Sin activar"
        elif not r["is_active"]:
            estado = "❌ Revocada"
        else:
            exp = datetime.fromisoformat(r["expires_at"])
            if now > exp + timedelta(hours=24):
                estado = "💀 Vencida"
            elif now > exp:
                estado = "⚠️ En gracia"
            else:
                estado = f"✅ {(exp - now).days}d"
        lineas.append(f"<code>{r['code']}</code> {r['chat_name'] or '—'} — {estado}")
    await update.message.reply_text(
        f"📋 <b>Licencias ({len(rows)}):</b>\n\n" + "\n".join(lineas), parse_mode="HTML"
    )


async def cmd_activar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_tg(update, context):
        return
    if not context.args:
        await update.message.reply_text(
            "Uso: <code>/activar &lt;código&gt;</code>", parse_mode="HTML"
        )
        return
    code = context.args[0].upper().strip()
    chat = update.effective_chat
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM licenses WHERE code=? AND chat_id IS NULL AND is_active=1", (code,)
        ).fetchone()
        if not row:
            already = conn.execute(
                "SELECT chat_id FROM licenses WHERE code=?", (code,)
            ).fetchone()
            await update.message.reply_text(
                "❌ Código ya utilizado." if already else "❌ Código inválido."
            )
            return
        now        = datetime.now()
        expires_at = now + timedelta(days=row["days"])
        conn.execute(
            "UPDATE licenses SET chat_id=?, chat_name=?, activated_at=?, expires_at=? WHERE code=?",
            (chat.id, chat.title or chat.username, now.isoformat(), expires_at.isoformat(), code)
        )
    await update.message.reply_text(
        f"✅ <b>¡Bot activado!</b>\n\n"
        f"📅 Vence: <b>{expires_at.strftime('%d/%m/%Y')}</b>\n"
        f"Escribí /menu para ver las opciones.", parse_mode="HTML"
    )

# ─── Utilidades ───────────────────────────────────────────────────────────────

async def is_admin_tg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    if chat.type == "private":
        return update.effective_user.id == ADMIN_ID
    try:
        m = await context.bot.get_chat_member(chat.id, update.effective_user.id)
        return m.status in ("administrator", "creator")
    except Exception:
        return False


async def get_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.reply_to_message:
        return update.message.reply_to_message.from_user
    if context.args:
        raw = context.args[0].lstrip("@")
        try:
            m = await context.bot.get_chat_member(update.effective_chat.id, raw)
            return m.user
        except Exception:
            pass
    return None


def user_link(user) -> str:
    return f'<a href="tg://user?id={user.id}">{user.full_name}</a>'


def fmt_hours(hours: float) -> str:
    if hours < 1:
        return f"{int(hours * 60)} min"
    return f"{hours:g} hora{'s' if hours != 1 else ''}"


def get_subgroups(leader_chat_id: int) -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT sub_chat_id, sub_name FROM subgroups WHERE leader_chat_id=?",
            (leader_chat_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def is_leader(chat_id: int) -> bool:
    with get_db() as conn:
        return bool(conn.execute(
            "SELECT 1 FROM leader_groups WHERE chat_id=?", (chat_id,)
        ).fetchone())

# ─── Zona horaria ─────────────────────────────────────────────────────────────

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin comparte ubicación → detecta y guarda zona horaria del grupo."""
    if not await is_admin_tg(update, context):
        return
    if not HAS_TF:
        await update.message.reply_text(
            "⚠️ Instalá <code>timezonefinder</code> para usar esta función:\n"
            "<code>pip install timezonefinder</code>", parse_mode="HTML"
        )
        return
    loc     = update.message.location
    tz_name = _TF.timezone_at(lat=loc.latitude, lng=loc.longitude)
    if not tz_name:
        await update.message.reply_text("❌ No se pudo detectar la zona horaria.")
        return
    chat_id = update.effective_chat.id
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO group_settings (chat_id, timezone) VALUES (?, ?)",
            (chat_id, tz_name)
        )
    await update.message.reply_text(
        f"🌍 Zona horaria configurada: <b>{tz_name}</b>\n"
        f"Los anuncios nuevos usarán esta zona horaria.", parse_mode="HTML"
    )

# ─── Menú Inline ─────────────────────────────────────────────────────────────

def kb_main():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📢 Anuncios",    callback_data="menu:ann"),
            InlineKeyboardButton("🚫 Palabras",    callback_data="menu:words"),
        ],
        [
            InlineKeyboardButton("⚔️ Moderación", callback_data="menu:mod"),
            InlineKeyboardButton("👥 Sub-grupos",  callback_data="menu:groups"),
        ],
        [InlineKeyboardButton("📋 Licencia",       callback_data="menu:license")],
        [InlineKeyboardButton("✅ Cerrar",          callback_data="menu:close")],
    ])


def kb_words():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Añadir local",  callback_data="words:add_local"),
            InlineKeyboardButton("🌐 Añadir global", callback_data="words:add_global"),
        ],
        [
            InlineKeyboardButton("📋 Ver lista",     callback_data="words:list"),
            InlineKeyboardButton("🗑️ Eliminar",      callback_data="words:delete"),
        ],
        [InlineKeyboardButton("⬅️ Volver",           callback_data="menu:main")],
    ])


def kb_mod():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👢 Kick",  callback_data="mod:kick"),
            InlineKeyboardButton("🚫 Ban",   callback_data="mod:ban"),
        ],
        [
            InlineKeyboardButton("🔇 Mute",  callback_data="mod:mute"),
            InlineKeyboardButton("🔊 Unmute",callback_data="mod:unmute"),
        ],
        [InlineKeyboardButton("✅ Unban",    callback_data="mod:unban")],
        [InlineKeyboardButton("⬅️ Volver",   callback_data="menu:main")],
    ])


def kb_ann():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Crear anuncio",    callback_data="ann:create")],
        [InlineKeyboardButton("📋 Ver anuncios",     callback_data="ann:list")],
        [InlineKeyboardButton("👁 Vista previa",     callback_data="ann:preview")],
        [
            InlineKeyboardButton("✏️ Editar",        callback_data="ann:edit"),
            InlineKeyboardButton("🗑️ Eliminar",      callback_data="ann:delete"),
        ],
        [InlineKeyboardButton("⬅️ Volver",           callback_data="menu:main")],
    ])


def kb_groups():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👑 Marcar como líder",        callback_data="groups:setlider")],
        [InlineKeyboardButton("🗑️ Eliminar grupo líder",     callback_data="groups:removelider")],
        [InlineKeyboardButton("➕ Registrar como sub-grupo",  callback_data="groups:addme")],
        [InlineKeyboardButton("📋 Ver sub-grupos",           callback_data="groups:list")],
        [InlineKeyboardButton("🗑️ Eliminar sub-grupo",       callback_data="groups:delete")],
        [InlineKeyboardButton("⬅️ Volver",                   callback_data="menu:main")],
    ])


def kb_back(dest: str):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data=dest)]])


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_tg(update, context):
        await update.message.reply_text("⛔ Solo los administradores pueden usar /menu.")
        return
    if not await require_license(update, context):
        return
    await update.message.reply_text(
        "⚙️ <b>Panel de configuración</b>\n\nElegí una sección:",
        reply_markup=kb_main(), parse_mode="HTML"
    )


async def cb_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    if not await is_admin_tg(update, context):
        await q.answer("Solo los administradores pueden usar esto.", show_alert=True)
        return

    chat_id = update.effective_chat.id

    if data == "menu:main":
        await q.edit_message_text(
            "⚙️ <b>Panel de configuración</b>\n\nElegí una sección:",
            reply_markup=kb_main(), parse_mode="HTML"
        )

    elif data == "menu:words":
        await q.edit_message_text(
            "🚫 <b>Palabras Prohibidas</b>\n\n"
            "• <b>Local</b>: solo este grupo\n"
            "• <b>Global</b>: este grupo y todos sus sub-grupos",
            reply_markup=kb_words(), parse_mode="HTML"
        )

    elif data == "menu:mod":
        await q.edit_message_text(
            "⚔️ <b>Moderación</b>\n\nRespondé el mensaje del usuario antes de usar kick/ban/mute.",
            reply_markup=kb_mod(), parse_mode="HTML"
        )

    elif data == "menu:ann":
        tz = get_group_tz(chat_id)
        await q.edit_message_text(
            f"📢 <b>Anuncios Programados</b>\n🌍 Zona horaria: <code>{tz}</code>",
            reply_markup=kb_ann(), parse_mode="HTML"
        )

    elif data == "menu:groups":
        estado = "👑 Este grupo es <b>líder</b>" if is_leader(chat_id) else "Sin grupo líder configurado aquí"
        await q.edit_message_text(
            f"👥 <b>Sub-grupos</b>\n\n{estado}\n\n"
            f"También podés usar comandos:\n"
            f"<code>/mainconnect &lt;id&gt;</code>\n"
            f"<code>/subconnect &lt;id&gt;</code>",
            reply_markup=kb_groups(), parse_mode="HTML"
        )

    elif data == "menu:license":
        with get_db() as conn:
            row = conn.execute(
                "SELECT expires_at FROM licenses WHERE chat_id=? AND is_active=1", (chat_id,)
            ).fetchone()
        if row:
            exp  = datetime.fromisoformat(row["expires_at"])
            rest = max(0, (exp - datetime.now()).days)
            txt  = (
                f"📋 <b>Licencia activa</b>\n\n"
                f"📅 Vence: <b>{exp.strftime('%d/%m/%Y')}</b>\n"
                f"⏱ Días restantes: <b>{rest}</b>"
            )
        else:
            txt = "📋 Sin licencia activa.\n\nUsá /activar &lt;código&gt; para activar."
        await q.edit_message_text(txt, reply_markup=kb_back("menu:main"), parse_mode="HTML")

    elif data == "menu:close":
        await q.message.delete()

    elif data == "words:list":
        with get_db() as conn:
            loc  = conn.execute(
                "SELECT word FROM forbidden_words WHERE chat_id=? AND is_global=0 ORDER BY word",
                (chat_id,)
            ).fetchall()
            glob = conn.execute(
                "SELECT word FROM forbidden_words WHERE chat_id=? AND is_global=1 ORDER BY word",
                (chat_id,)
            ).fetchall()
        txt = "🚫 <b>Palabras prohibidas</b>\n\n"
        if loc:
            txt += "📍 <b>Locales:</b>\n" + "\n".join(f"  • <code>{r['word']}</code>" for r in loc) + "\n\n"
        if glob:
            txt += "🌐 <b>Globales:</b>\n" + "\n".join(f"  • <code>{r['word']}</code>" for r in glob)
        if not loc and not glob:
            txt += "No hay palabras registradas."
        await q.edit_message_text(txt, reply_markup=kb_back("menu:words"), parse_mode="HTML")

    elif data.startswith("mod:"):
        instrucciones = {
            "mod:kick":   "👢 Respondé el mensaje del usuario y escribí /kick",
            "mod:ban":    "🚫 Respondé el mensaje y escribí /ban [motivo]",
            "mod:mute":   "🔇 Respondé el mensaje y escribí /mute [minutos]",
            "mod:unmute": "🔊 Respondé el mensaje y escribí /unmute",
            "mod:unban":  "✅ Escribí /unban @usuario",
        }
        await q.answer(instrucciones.get(data, "Usá el comando correspondiente."), show_alert=True)

    elif data == "ann:list":
        with get_db() as conn:
            rows = conn.execute(
                "SELECT id, text, media_type, interval_hours, start_time FROM announcements WHERE chat_id=?",
                (chat_id,)
            ).fetchall()
        if not rows:
            txt = "📢 No hay anuncios programados."
        else:
            lineas = []
            for r in rows:
                prev = re.sub(r'<[^>]+>', '', r["text"])[:35].replace("\n", " ") + "…"
                icon = {"photo": "📷", "video": "🎥"}.get(r["media_type"], "📝")
                hora = f" · ⏰ {r['start_time']}" if r["start_time"] else ""
                lineas.append(f"<b>#{r['id']}</b> {icon} {prev} · {fmt_hours(r['interval_hours'])}{hora}")
            txt = "📢 <b>Anuncios:</b>\n\n" + "\n".join(lineas)
        await q.edit_message_text(txt, reply_markup=kb_back("menu:ann"), parse_mode="HTML")

    # ann:create is handled by conv_announce entry point below

    elif data == "groups:setlider":
        if update.effective_chat.type == "private":
            await q.answer("❌ Solo se puede establecer un grupo como líder, no un chat privado.", show_alert=True)
            return
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO leader_groups (chat_id, set_at) VALUES (?, ?)",
                (chat_id, datetime.now().isoformat())
            )
        await q.edit_message_text(
            "👑 <b>¡Este grupo ahora es el líder!</b>\n\n"
            "Los anuncios y palabras globales se replicarán a sus sub-grupos.",
            reply_markup=kb_back("menu:groups"), parse_mode="HTML"
        )

    elif data == "groups:removelider":
        with get_db() as conn:
            conn.execute("DELETE FROM leader_groups WHERE chat_id=?", (chat_id,))
        await q.edit_message_text(
            "🗑️ <b>Grupo líder eliminado.</b>\nEste grupo ya no es el líder.",
            reply_markup=kb_back("menu:groups"), parse_mode="HTML"
        )

    elif data == "groups:addme":
        if update.effective_chat.type == "private":
            await q.answer("❌ Solo se puede registrar un grupo como sub-grupo, no un chat privado.", show_alert=True)
            return
        with get_db() as conn:
            lider = conn.execute("SELECT chat_id FROM leader_groups LIMIT 1").fetchone()
        if not lider:
            await q.answer("❌ No hay grupo líder configurado.", show_alert=True)
            return
        if chat_id == lider["chat_id"]:
            await q.answer("❌ Este grupo ya es el líder.", show_alert=True)
            return
        chat = update.effective_chat
        with get_db() as conn:
            try:
                conn.execute(
                    "INSERT INTO subgroups (leader_chat_id, sub_chat_id, sub_name) VALUES (?, ?, ?)",
                    (lider["chat_id"], chat_id, chat.title or chat.username)
                )
                await q.edit_message_text(
                    "✅ <b>¡Grupo registrado como sub-grupo!</b>",
                    reply_markup=kb_back("menu:groups"), parse_mode="HTML"
                )
            except sqlite3.IntegrityError:
                await q.answer("⚠️ Ya está registrado como sub-grupo.", show_alert=True)

    elif data == "groups:list":
        subs = get_subgroups(chat_id)
        if not subs:
            txt = "👥 No hay sub-grupos registrados."
        else:
            lineas = [
                f"• <b>{s['sub_name'] or 'Sin nombre'}</b> (<code>{s['sub_chat_id']}</code>)"
                for s in subs
            ]
            txt = f"👥 <b>Sub-grupos ({len(subs)}):</b>\n\n" + "\n".join(lineas)
        await q.edit_message_text(txt, reply_markup=kb_back("menu:groups"), parse_mode="HTML")

# ─── ConversationHandler: inputs desde el menú ───────────────────────────────

async def cb_word_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()
    tipo = "local" if data == "words:add_local" else "global"
    context.user_data["word_tipo"] = tipo
    desc = "solo en este grupo" if tipo == "local" else "en este grupo y sus sub-grupos"
    await q.edit_message_text(
        f"✍️ Escribí la palabra a prohibir (<b>{desc}</b>):\n\n/cancelar para volver.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="menu:words")]])
    )
    return WORD_ADD_WAIT


async def cb_word_add_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    word    = update.message.text.strip().lower()
    tipo    = context.user_data.get("word_tipo", "local")
    chat_id = update.effective_chat.id
    is_glob = 1 if tipo == "global" else 0
    with get_db() as conn:
        try:
            conn.execute(
                "INSERT INTO forbidden_words (chat_id, word, is_global) VALUES (?, ?, ?)",
                (chat_id, word, is_glob)
            )
        except sqlite3.IntegrityError:
            pass
    if is_glob:
        for sub in get_subgroups(chat_id):
            with get_db() as conn:
                try:
                    conn.execute(
                        "INSERT INTO forbidden_words (chat_id, word, is_global) VALUES (?, ?, 1)",
                        (sub["sub_chat_id"], word)
                    )
                except sqlite3.IntegrityError:
                    pass
    scope = "todos los grupos" if is_glob else "este grupo"
    await update.message.reply_text(
        f"✅ <code>{word}</code> añadida para <b>{scope}</b>.", parse_mode="HTML", reply_markup=kb_words()
    )
    return ConversationHandler.END


async def cb_word_del_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "✍️ Escribí la palabra a eliminar:\n\n/cancelar para volver.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="menu:words")]])
    )
    return WORD_DEL_WAIT


async def cb_word_del_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    word = update.message.text.strip().lower()
    with get_db() as conn:
        conn.execute(
            "DELETE FROM forbidden_words WHERE chat_id=? AND word=?", (update.effective_chat.id, word)
        )
    await update.message.reply_text(
        f"🗑️ <code>{word}</code> eliminada.", parse_mode="HTML", reply_markup=kb_words()
    )
    return ConversationHandler.END


async def cb_ann_preview_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "✍️ Escribí el <b>ID</b> del anuncio:", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="menu:ann")]])
    )
    return ANN_PREVIEW_WAIT


async def cb_ann_preview_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        ann_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Ingresá un número válido.")
        return ANN_PREVIEW_WAIT
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM announcements WHERE id=? AND chat_id=?",
            (ann_id, update.effective_chat.id)
        ).fetchone()
    if not row:
        await update.message.reply_text(f"No encontré el anuncio #{ann_id}.", reply_markup=kb_ann())
    else:
        await update.message.reply_text("👁 Vista previa:")
        await _enviar_anuncio(
            context.bot, update.effective_chat.id,
            row["text"], row["media_file_id"], row["media_type"], row["buttons_json"]
        )
    return ConversationHandler.END


async def cb_ann_del_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "✍️ Escribí el <b>ID</b> del anuncio a eliminar:", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="menu:ann")]])
    )
    return ANN_DEL_WAIT


async def cb_ann_del_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        ann_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Ingresá un número válido.")
        return ANN_DEL_WAIT
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM announcements WHERE id=? AND chat_id=?",
            (ann_id, update.effective_chat.id)
        ).fetchone()
        if not row:
            await update.message.reply_text(f"No encontré el anuncio #{ann_id}.", reply_markup=kb_ann())
            return ConversationHandler.END
        conn.execute("DELETE FROM announcements WHERE id=?", (ann_id,))
    try:
        scheduler.remove_job(f"ann_{ann_id}")
    except Exception:
        pass
    await update.message.reply_text(
        f"🗑️ Anuncio <b>#{ann_id}</b> eliminado.", parse_mode="HTML", reply_markup=kb_ann()
    )
    return ConversationHandler.END


async def cb_sub_del_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    subs = get_subgroups(update.effective_chat.id)
    if not subs:
        await q.edit_message_text("No hay sub-grupos para eliminar.", reply_markup=kb_back("menu:groups"))
        return ConversationHandler.END
    lineas = [f"• <code>{s['sub_chat_id']}</code> — {s['sub_name'] or 'Sin nombre'}" for s in subs]
    await q.edit_message_text(
        "✍️ Escribí el <b>chat_id</b> del sub-grupo a eliminar:\n\n" + "\n".join(lineas),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="menu:groups")]])
    )
    return SUB_DEL_WAIT


async def cb_sub_del_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        sub_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Ingresá un número válido.")
        return SUB_DEL_WAIT
    with get_db() as conn:
        conn.execute(
            "DELETE FROM subgroups WHERE leader_chat_id=? AND sub_chat_id=?",
            (update.effective_chat.id, sub_id)
        )
    await update.message.reply_text("🗑️ Sub-grupo eliminado.", reply_markup=kb_groups())
    return ConversationHandler.END


async def conv_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelado.", reply_markup=kb_main())
    return ConversationHandler.END

# ─── Editar anuncio ───────────────────────────────────────────────────────────

async def cb_ann_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = update.effective_chat.id
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, text, interval_hours, start_time FROM announcements WHERE chat_id=?",
            (chat_id,)
        ).fetchall()
    if not rows:
        await q.edit_message_text("No hay anuncios para editar.", reply_markup=kb_back("menu:ann"))
        return ConversationHandler.END
    lineas = []
    for r in rows:
        prev = re.sub(r'<[^>]+>', '', r["text"])[:30].replace("\n", " ")
        hora = f" · ⏰ {r['start_time']}" if r["start_time"] else ""
        lineas.append(f"<b>#{r['id']}</b> {prev}… · {fmt_hours(r['interval_hours'])}{hora}")
    await q.edit_message_text(
        "✏️ <b>Editar anuncio</b>\n\nEscribí el ID:\n\n" + "\n".join(lineas),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="menu:ann")]])
    )
    return ANN_EDIT_ID


async def ann_edit_get_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        ann_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Ingresá un número válido.")
        return ANN_EDIT_ID
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM announcements WHERE id=? AND chat_id=?",
            (ann_id, update.effective_chat.id)
        ).fetchone()
    if not row:
        await update.message.reply_text(f"No encontré el anuncio #{ann_id}.", reply_markup=kb_ann())
        return ConversationHandler.END
    context.user_data["edit_id"] = ann_id
    prev = re.sub(r'<[^>]+>', '', row["text"])[:60]
    await update.message.reply_text(
        f"✏️ <b>Anuncio #{ann_id}</b>\n"
        f"📝 Texto: <i>{prev}…</i>\n"
        f"🕐 Intervalo: cada {fmt_hours(row['interval_hours'])}\n"
        f"⏰ Hora de inicio: {row['start_time'] or 'Inmediato'}\n\n"
        f"¿Qué querés cambiar?\n"
        f"• <code>texto</code>\n"
        f"• <code>intervalo</code>\n"
        f"• <code>horario</code>\n\n"
        f"/cancelar para salir.",
        parse_mode="HTML"
    )
    return ANN_EDIT_FIELD


async def ann_edit_get_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = update.message.text.strip().lower()
    if field not in ("texto", "intervalo", "horario"):
        await update.message.reply_text(
            "Escribí <code>texto</code>, <code>intervalo</code> o <code>horario</code>.",
            parse_mode="HTML"
        )
        return ANN_EDIT_FIELD
    context.user_data["edit_field"] = field
    prompts = {
        "texto":     "✍️ Escribí el nuevo texto del anuncio:",
        "intervalo": "⏱ Escribí el nuevo intervalo en horas (ej: <code>1</code>, <code>0.5</code>, <code>24</code>):",
        "horario":   "⏰ Escribí la hora en formato <code>HH:MM</code> (ej: <code>18:00</code>)\nO <code>ahora</code> para inicio inmediato:",
    }
    await update.message.reply_text(prompts[field], parse_mode="HTML")
    return ANN_EDIT_VALUE


async def ann_edit_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ann_id  = context.user_data["edit_id"]
    field   = context.user_data["edit_field"]
    val     = update.message.text.strip()
    chat_id = update.effective_chat.id

    with get_db() as conn:
        row = conn.execute("SELECT * FROM announcements WHERE id=?", (ann_id,)).fetchone()

    if field == "texto":
        new_text = update.message.text_html or val
        with get_db() as conn:
            conn.execute("UPDATE announcements SET text=? WHERE id=?", (new_text, ann_id))
        _programar_anuncio(
            context.application, ann_id, row["chat_id"],
            new_text, row["media_file_id"], row["media_type"],
            row["buttons_json"], row["interval_hours"], row["start_time"], chat_id
        )
        await update.message.reply_text(
            f"✅ Texto del anuncio <b>#{ann_id}</b> actualizado.", parse_mode="HTML", reply_markup=kb_ann()
        )

    elif field == "intervalo":
        try:
            horas = float(val.replace(",", "."))
            if horas <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Número inválido. Intentá de nuevo.")
            return ANN_EDIT_VALUE
        with get_db() as conn:
            conn.execute("UPDATE announcements SET interval_hours=? WHERE id=?", (horas, ann_id))
        _programar_anuncio(
            context.application, ann_id, row["chat_id"],
            row["text"], row["media_file_id"], row["media_type"],
            row["buttons_json"], horas, row["start_time"], chat_id
        )
        await update.message.reply_text(
            f"✅ Intervalo del anuncio <b>#{ann_id}</b>: cada {fmt_hours(horas)}.",
            parse_mode="HTML", reply_markup=kb_ann()
        )

    elif field == "horario":
        if val.lower() in ("ahora", "now"):
            start_time = None
        else:
            if not re.match(r'^\d{1,2}:\d{2}$', val):
                await update.message.reply_text(
                    "Formato inválido. Usá <code>HH:MM</code> o <code>ahora</code>.", parse_mode="HTML"
                )
                return ANN_EDIT_VALUE
            start_time = val
        with get_db() as conn:
            conn.execute("UPDATE announcements SET start_time=? WHERE id=?", (start_time, ann_id))
        _programar_anuncio(
            context.application, ann_id, row["chat_id"],
            row["text"], row["media_file_id"], row["media_type"],
            row["buttons_json"], row["interval_hours"], start_time, chat_id
        )
        txt_hora = start_time if start_time else "inmediatamente"
        await update.message.reply_text(
            f"✅ Hora de inicio del anuncio <b>#{ann_id}</b>: {txt_hora}.",
            parse_mode="HTML", reply_markup=kb_ann()
        )

    return ConversationHandler.END

# ─── Palabras Prohibidas (comandos) ──────────────────────────────────────────

async def cmd_addword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_tg(update, context):
        return
    if not await require_license(update, context):
        return
    if not context.args:
        await update.message.reply_text("Uso: /addword <palabra>")
        return
    word    = " ".join(context.args).lower().strip()
    chat_id = update.effective_chat.id
    with get_db() as conn:
        try:
            conn.execute(
                "INSERT INTO forbidden_words (chat_id, word, is_global) VALUES (?, ?, 0)",
                (chat_id, word)
            )
            await update.message.reply_text(
                f"✅ Palabra local añadida: <code>{word}</code>", parse_mode="HTML"
            )
        except sqlite3.IntegrityError:
            await update.message.reply_text(
                f"⚠️ <code>{word}</code> ya estaba en la lista.", parse_mode="HTML"
            )


async def cmd_addwordglobal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_tg(update, context):
        return
    if not await require_license(update, context):
        return
    if not context.args:
        await update.message.reply_text("Uso: /addwordglobal <palabra>")
        return
    word    = " ".join(context.args).lower().strip()
    chat_id = update.effective_chat.id
    with get_db() as conn:
        try:
            conn.execute(
                "INSERT INTO forbidden_words (chat_id, word, is_global) VALUES (?, ?, 1)",
                (chat_id, word)
            )
        except sqlite3.IntegrityError:
            pass
    for sub in get_subgroups(chat_id):
        with get_db() as conn:
            try:
                conn.execute(
                    "INSERT INTO forbidden_words (chat_id, word, is_global) VALUES (?, ?, 1)",
                    (sub["sub_chat_id"], word)
                )
            except sqlite3.IntegrityError:
                pass
    await update.message.reply_text(
        f"🌐 Palabra global añadida: <code>{word}</code>", parse_mode="HTML"
    )


async def cmd_removeword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_tg(update, context):
        return
    if not await require_license(update, context):
        return
    if not context.args:
        await update.message.reply_text("Uso: /removeword <palabra>")
        return
    word = " ".join(context.args).lower().strip()
    with get_db() as conn:
        conn.execute(
            "DELETE FROM forbidden_words WHERE chat_id=? AND word=?",
            (update.effective_chat.id, word)
        )
    await update.message.reply_text(
        f"🗑️ Palabra eliminada: <code>{word}</code>", parse_mode="HTML"
    )


async def cmd_listwords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_tg(update, context):
        return
    if not await require_license(update, context):
        return
    chat_id = update.effective_chat.id
    with get_db() as conn:
        loc  = conn.execute(
            "SELECT word FROM forbidden_words WHERE chat_id=? AND is_global=0 ORDER BY word",
            (chat_id,)
        ).fetchall()
        glob = conn.execute(
            "SELECT word FROM forbidden_words WHERE chat_id=? AND is_global=1 ORDER BY word",
            (chat_id,)
        ).fetchall()
    txt = "🚫 <b>Palabras prohibidas</b>\n\n"
    if loc:
        txt += "📍 <b>Locales:</b>\n" + "\n".join(f"• <code>{r['word']}</code>" for r in loc) + "\n\n"
    if glob:
        txt += "🌐 <b>Globales:</b>\n" + "\n".join(f"• <code>{r['word']}</code>" for r in glob)
    if not loc and not glob:
        txt += "No hay palabras registradas."
    await update.message.reply_text(txt, parse_mode="HTML")


async def filtrar_mensajes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    ok, _ = await check_license(update.effective_chat.id)
    if not ok:
        return
    chat_id = update.effective_chat.id
    user    = update.effective_user
    m       = await context.bot.get_chat_member(chat_id, user.id)
    if m.status in ("administrator", "creator"):
        return
    text = update.message.text.lower()
    with get_db() as conn:
        palabras = [r["word"] for r in conn.execute(
            "SELECT word FROM forbidden_words WHERE chat_id=?", (chat_id,)
        ).fetchall()]
    if not any(p in text for p in palabras):
        return
    try:
        await update.message.delete()
    except Exception:
        pass
    with get_db() as conn:
        conn.execute(
            "INSERT INTO warnings (chat_id, user_id, count) VALUES (?,?,1) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET count=count+1",
            (chat_id, user.id)
        )
        adv = conn.execute(
            "SELECT count FROM warnings WHERE chat_id=? AND user_id=?",
            (chat_id, user.id)
        ).fetchone()["count"]
    if adv >= 3:
        await context.bot.restrict_chat_member(
            chat_id, user.id, ChatPermissions(can_send_messages=False)
        )
        with get_db() as conn:
            conn.execute(
                "UPDATE warnings SET count=0 WHERE chat_id=? AND user_id=?", (chat_id, user.id)
            )
        await context.bot.send_message(
            chat_id,
            f"🔇 {user_link(user)} fue <b>silenciado</b> por acumular 3 advertencias.",
            parse_mode="HTML"
        )
    else:
        await context.bot.send_message(
            chat_id,
            f"⚠️ {user_link(user)}, mensaje eliminado por palabras prohibidas. "
            f"Advertencia <b>{adv}/3</b>.",
            parse_mode="HTML"
        )

# ─── Moderación ───────────────────────────────────────────────────────────────

async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_tg(update, context):
        return
    if not await require_license(update, context):
        return
    target = await get_target(update, context)
    if not target:
        await update.message.reply_text("Respondé el mensaje del usuario o escribí /kick @usuario")
        return
    chat_id = update.effective_chat.id
    await context.bot.ban_chat_member(chat_id, target.id)
    await context.bot.unban_chat_member(chat_id, target.id)
    await update.message.reply_text(
        f"👢 {user_link(target)} fue <b>expulsado</b>.", parse_mode="HTML"
    )


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_tg(update, context):
        return
    if not await require_license(update, context):
        return
    target = await get_target(update, context)
    if not target:
        await update.message.reply_text("Respondé el mensaje o escribí /ban @usuario [motivo]")
        return
    args   = context.args or []
    start  = 1 if args and args[0].startswith("@") else 0
    motivo = " ".join(args[start:]) if len(args) > start else "Sin motivo"
    await context.bot.ban_chat_member(update.effective_chat.id, target.id)
    await update.message.reply_text(
        f"🚫 {user_link(target)} fue <b>baneado</b>.\n📝 {motivo}", parse_mode="HTML"
    )


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_tg(update, context):
        return
    if not await require_license(update, context):
        return
    target = await get_target(update, context)
    if not target:
        await update.message.reply_text("Respondé el mensaje o escribí /unban @usuario")
        return
    await context.bot.unban_chat_member(update.effective_chat.id, target.id)
    await update.message.reply_text(
        f"✅ {user_link(target)} fue <b>desbaneado</b>.", parse_mode="HTML"
    )


async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_tg(update, context):
        return
    if not await require_license(update, context):
        return
    target = await get_target(update, context)
    if not target:
        await update.message.reply_text("Respondé el mensaje o escribí /mute @usuario [minutos]")
        return
    until_date = None
    minutos    = None
    if context.args:
        try:
            minutos    = int(context.args[-1])
            until_date = datetime.now() + timedelta(minutes=minutos)
        except ValueError:
            pass
    await context.bot.restrict_chat_member(
        update.effective_chat.id, target.id,
        ChatPermissions(can_send_messages=False),
        until_date=until_date,
    )
    dur = f"por {minutos} min" if minutos else "indefinidamente"
    await update.message.reply_text(
        f"🔇 {user_link(target)} silenciado {dur}.", parse_mode="HTML"
    )


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_tg(update, context):
        return
    if not await require_license(update, context):
        return
    target = await get_target(update, context)
    if not target:
        await update.message.reply_text("Respondé el mensaje o escribí /unmute @usuario")
        return
    await context.bot.restrict_chat_member(
        update.effective_chat.id, target.id,
        ChatPermissions(
            can_send_messages=True,
            can_send_audios=True,
            can_send_documents=True,
            can_send_photos=True,
            can_send_videos=True,
            can_send_video_notes=True,
            can_send_voice_notes=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
            can_invite_users=True,
        ),
    )
    await update.message.reply_text(
        f"🔊 {user_link(target)} puede hablar nuevamente.", parse_mode="HTML"
    )

# ─── Grupos por ID (solo admin del bot) ──────────────────────────────────────

async def cmd_mainconnect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/mainconnect <group_id> — establece grupo líder por ID"""
    if not is_bot_admin(update):
        await update.message.reply_text("⛔ Solo el administrador del bot puede usar este comando.")
        return
    if not context.args:
        await update.message.reply_text("Uso: /mainconnect <group_id>")
        return
    try:
        group_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("El ID debe ser un número (ej: -1001234567890)")
        return
    try:
        chat = await context.bot.get_chat(group_id)
        if chat.type == "private":
            await update.message.reply_text("❌ Solo se puede establecer un grupo como líder, no un chat privado.")
            return
        name = chat.title or str(group_id)
    except Exception:
        await update.message.reply_text(
            f"❌ No pude acceder a <code>{group_id}</code>. Asegurate de que el bot esté en ese grupo.",
            parse_mode="HTML"
        )
        return
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO leader_groups (chat_id, set_at) VALUES (?, ?)",
            (group_id, datetime.now().isoformat())
        )
    await update.message.reply_text(
        f"👑 <b>{name}</b> (<code>{group_id}</code>) establecido como grupo líder.",
        parse_mode="HTML"
    )
    try:
        await context.bot.send_message(
            group_id,
            "👑 <b>Este grupo fue configurado como grupo líder.</b>\n"
            "Los anuncios y palabras globales se replicarán a sus sub-grupos.",
            parse_mode="HTML"
        )
    except Exception:
        pass


async def cmd_subconnect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/subconnect <group_id> — registra sub-grupo por ID"""
    if not is_bot_admin(update):
        await update.message.reply_text("⛔ Solo el administrador del bot puede usar este comando.")
        return
    if not context.args:
        await update.message.reply_text("Uso: /subconnect <group_id>")
        return
    try:
        group_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("El ID debe ser un número (ej: -1001234567890)")
        return
    with get_db() as conn:
        lider = conn.execute("SELECT chat_id FROM leader_groups LIMIT 1").fetchone()
    if not lider:
        await update.message.reply_text("❌ No hay grupo líder. Usá /mainconnect primero.")
        return
    if group_id == lider["chat_id"]:
        await update.message.reply_text("❌ Ese grupo ya es el líder.")
        return
    try:
        chat = await context.bot.get_chat(group_id)
        if chat.type == "private":
            await update.message.reply_text("❌ Solo se puede registrar un grupo como sub-grupo.")
            return
        name = chat.title or str(group_id)
    except Exception:
        await update.message.reply_text(
            f"❌ No pude acceder a <code>{group_id}</code>. Asegurate de que el bot esté en ese grupo.",
            parse_mode="HTML"
        )
        return
    with get_db() as conn:
        try:
            conn.execute(
                "INSERT INTO subgroups (leader_chat_id, sub_chat_id, sub_name) VALUES (?, ?, ?)",
                (lider["chat_id"], group_id, name)
            )
        except sqlite3.IntegrityError:
            await update.message.reply_text(
                f"⚠️ <b>{name}</b> ya está registrado como sub-grupo.", parse_mode="HTML"
            )
            return
    await update.message.reply_text(
        f"✅ <b>{name}</b> (<code>{group_id}</code>) registrado como sub-grupo.", parse_mode="HTML"
    )
    try:
        await context.bot.send_message(
            group_id,
            "✅ <b>Este grupo fue registrado como sub-grupo.</b>\n"
            "Recibirá los anuncios y palabras globales del grupo líder.",
            parse_mode="HTML"
        )
    except Exception:
        pass

# ─── Anuncios ─────────────────────────────────────────────────────────────────

async def _announce_start(chat_id: int, reply_fn):
    """Lógica compartida para iniciar la creación de un anuncio."""
    return await reply_fn(
        "📢 <b>Crear anuncio programado</b>\n\n"
        "<b>Paso 1/5</b> — Escribí el texto del anuncio.\n\n/cancelar para salir.",
        parse_mode="HTML"
    )


async def cb_ann_create_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point desde el botón ➕ Crear anuncio."""
    q = update.callback_query
    await q.answer()
    if not await is_admin_tg(update, context):
        await q.answer("Solo los administradores pueden hacer esto.", show_alert=True)
        return ConversationHandler.END
    if not await require_license(update, context):
        return ConversationHandler.END
    context.user_data["ann"] = {"chat_id": update.effective_chat.id}
    await q.edit_message_text(
        "📢 <b>Crear anuncio programado</b>\n\n"
        "<b>Paso 1/5</b> — Escribí el texto del anuncio.\n\n/cancelar para salir.",
        parse_mode="HTML"
    )
    return ANN_TEXT


async def cmd_announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_tg(update, context):
        return
    if not await require_license(update, context):
        return
    context.user_data["ann"] = {"chat_id": update.effective_chat.id}
    await update.message.reply_text(
        "📢 <b>Crear anuncio programado</b>\n\n"
        "<b>Paso 1/5</b> — Escribí el texto del anuncio.\n\n/cancelar para salir.",
        parse_mode="HTML"
    )
    return ANN_TEXT


async def ann_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["ann"]["text"] = update.message.text_html or update.message.text
    await update.message.reply_text(
        "<b>Paso 2/5</b> — Enviá una foto o video para adjuntar.\n"
        "Si no querés, escribí /skip", parse_mode="HTML"
    )
    return ANN_MEDIA


async def ann_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ann = context.user_data["ann"]
    if update.message.photo:
        ann["media_file_id"] = update.message.photo[-1].file_id
        ann["media_type"]    = "photo"
    elif update.message.video:
        ann["media_file_id"] = update.message.video.file_id
        ann["media_type"]    = "video"
    else:
        ann["media_file_id"] = None
        ann["media_type"]    = None
    await update.message.reply_text(
        "<b>Paso 3/5</b> — Agregá botones de enlace:\n"
        "<code>Texto | https://url.com</code> (uno por línea)\n\n/skip para omitir",
        parse_mode="HTML"
    )
    return ANN_BUTTONS


async def ann_botones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ann  = context.user_data["ann"]
    text = update.message.text or ""
    if text.strip() == "/skip":
        ann["buttons"] = []
    else:
        bots = []
        for linea in text.strip().splitlines():
            if "|" in linea:
                p = linea.split("|", 1)
                label, url = p[0].strip(), p[1].strip()
                if label and url.startswith("http"):
                    bots.append({"text": label, "url": url})
        ann["buttons"] = bots
        if not bots:
            await update.message.reply_text(
                "⚠️ Formato: <code>Texto | https://url.com</code>\nO escribí /skip",
                parse_mode="HTML"
            )
            return ANN_BUTTONS
    await update.message.reply_text(
        "<b>Paso 4/5</b> — ¿Cada cuántas horas repetir?\n\n"
        "<code>0.5</code> = 30 min · <code>1</code> = 1 hora · <code>24</code> = diario",
        parse_mode="HTML"
    )
    return ANN_INTERVAL


async def ann_intervalo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        horas = float(update.message.text.strip().replace(",", "."))
        if horas <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Ingresá un número válido (ej: 1, 2.5, 0.5)")
        return ANN_INTERVAL
    context.user_data["ann"]["interval_hours"] = horas
    tz = get_group_tz(update.effective_chat.id)
    await update.message.reply_text(
        f"<b>Paso 5/5</b> — ¿A qué hora querés que empiece?\n\n"
        f"Formato: <code>HH:MM</code> (ej: <code>18:00</code>)\n"
        f"🌍 Zona horaria: <code>{tz}</code>\n\n"
        f"O /skip para que empiece ya.",
        parse_mode="HTML"
    )
    return ANN_STARTTIME


async def ann_starttime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip()
    ann = context.user_data["ann"]
    if val == "/skip":
        ann["start_time"] = None
    else:
        if not re.match(r'^\d{1,2}:\d{2}$', val):
            await update.message.reply_text(
                "Formato inválido. Usá <code>HH:MM</code> (ej: <code>18:00</code>) o /skip",
                parse_mode="HTML"
            )
            return ANN_STARTTIME
        ann["start_time"] = val

    horas        = ann["interval_hours"]
    buttons_json = json.dumps(ann.get("buttons", []))
    chat_id      = ann["chat_id"]

    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO announcements "
            "(chat_id, text, media_file_id, media_type, buttons_json, interval_hours, start_time, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, ann["text"], ann.get("media_file_id"), ann.get("media_type"),
             buttons_json, horas, ann.get("start_time"), datetime.now().isoformat())
        )
        ann_id = cursor.lastrowid

    _programar_anuncio(
        context.application, ann_id, chat_id,
        ann["text"], ann.get("media_file_id"), ann.get("media_type"),
        buttons_json, horas, ann.get("start_time"), chat_id
    )

    icon   = {"photo": "📷", "video": "🎥"}.get(ann.get("media_type"), "📝")
    inicio = ann.get("start_time") or "Inmediato"
    await update.message.reply_text(
        f"✅ <b>Anuncio #{ann_id} creado</b>\n\n"
        f"{icon} · 🕐 cada {fmt_hours(horas)} · ⏰ {inicio}",
        parse_mode="HTML"
    )
    return ConversationHandler.END


async def ann_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelado.")
    return ConversationHandler.END


def _build_keyboard(buttons_json: str):
    try:
        buttons = json.loads(buttons_json) if buttons_json else []
    except Exception:
        buttons = []
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(b["text"], url=b["url"])] for b in buttons]
    )


async def _enviar_anuncio(bot, chat_id: int, text: str,
                           media_file_id, media_type, buttons_json: str):
    markup = _build_keyboard(buttons_json)
    try:
        if media_type == "photo":
            await bot.send_photo(chat_id, media_file_id, caption=text,
                                 reply_markup=markup, parse_mode="HTML")
        elif media_type == "video":
            await bot.send_video(chat_id, media_file_id, caption=text,
                                 reply_markup=markup, parse_mode="HTML")
        else:
            await bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error enviando anuncio al chat {chat_id}: {e}")


async def _enviar_anuncio_con_subs(bot, chat_id: int, text: str,
                                    media_file_id, media_type, buttons_json: str):
    await _enviar_anuncio(bot, chat_id, text, media_file_id, media_type, buttons_json)
    for sub in get_subgroups(chat_id):
        await _enviar_anuncio(bot, sub["sub_chat_id"], text, media_file_id, media_type, buttons_json)


def _get_start_date(start_time: str, tz_name: str):
    """Calcula la próxima ocurrencia de HH:MM en la zona horaria dada."""
    try:
        tz        = pytz.timezone(tz_name)
        now       = datetime.now(tz)
        h, m      = map(int, start_time.split(":"))
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    except Exception:
        return None


def _programar_anuncio(app, ann_id: int, chat_id: int, text: str,
                        media_file_id, media_type, buttons_json: str,
                        interval_hours: float, start_time=None, tz_chat_id=None):
    tz_name    = get_group_tz(tz_chat_id or chat_id)
    start_date = _get_start_date(start_time, tz_name) if start_time else None
    scheduler.add_job(
        _enviar_anuncio_con_subs,
        IntervalTrigger(hours=interval_hours, start_date=start_date, timezone=tz_name),
        args=[app.bot, chat_id, text, media_file_id, media_type, buttons_json],
        id=f"ann_{ann_id}",
        replace_existing=True,
    )
    inicio = f"desde {start_time}" if start_time else "inmediatamente"
    logger.info(f"Anuncio #{ann_id} programado cada {fmt_hours(interval_hours)} ({inicio}, tz={tz_name})")


async def cmd_listanuncios(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_tg(update, context):
        return
    if not await require_license(update, context):
        return
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, text, media_type, interval_hours, start_time FROM announcements WHERE chat_id=?",
            (update.effective_chat.id,)
        ).fetchall()
    if not rows:
        await update.message.reply_text("No hay anuncios programados.")
        return
    lineas = []
    for r in rows:
        prev  = re.sub(r'<[^>]+>', '', r["text"])[:45].replace("\n", " ") + "…"
        icon  = {"photo": "📷", "video": "🎥"}.get(r["media_type"], "📝")
        hora  = f" · ⏰ desde {r['start_time']}" if r["start_time"] else ""
        lineas.append(
            f"<b>#{r['id']}</b> {icon} {prev}\n     🕐 Cada {fmt_hours(r['interval_hours'])}{hora}"
        )
    await update.message.reply_text(
        f"📋 <b>Anuncios ({len(rows)}):</b>\n\n" + "\n\n".join(lineas) +
        "\n\nUsá <code>/delanuncio &lt;id&gt;</code> o /menu.",
        parse_mode="HTML"
    )


async def cmd_delanuncio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_tg(update, context):
        return
    if not await require_license(update, context):
        return
    if not context.args:
        await update.message.reply_text("Uso: /delanuncio <id>")
        return
    try:
        ann_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("El ID debe ser un número.")
        return
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM announcements WHERE id=? AND chat_id=?",
            (ann_id, update.effective_chat.id)
        ).fetchone()
        if not row:
            await update.message.reply_text(f"No encontré el anuncio #{ann_id}.")
            return
        conn.execute("DELETE FROM announcements WHERE id=?", (ann_id,))
    try:
        scheduler.remove_job(f"ann_{ann_id}")
    except Exception:
        pass
    await update.message.reply_text(
        f"🗑️ Anuncio <b>#{ann_id}</b> eliminado.", parse_mode="HTML"
    )


async def cmd_veranuncio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_tg(update, context):
        return
    if not await require_license(update, context):
        return
    if not context.args:
        await update.message.reply_text("Uso: /veranuncio <id>")
        return
    try:
        ann_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("El ID debe ser un número.")
        return
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM announcements WHERE id=? AND chat_id=?",
            (ann_id, update.effective_chat.id)
        ).fetchone()
    if not row:
        await update.message.reply_text(f"No encontré el anuncio #{ann_id}.")
        return
    await update.message.reply_text("👁 Vista previa:")
    await _enviar_anuncio(
        context.bot, update.effective_chat.id,
        row["text"], row["media_file_id"], row["media_type"], row["buttons_json"]
    )

# ─── Avisos de vencimiento ────────────────────────────────────────────────────

async def job_avisar_vencimientos(bot):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT chat_id, expires_at FROM licenses WHERE is_active=1 AND chat_id IS NOT NULL"
        ).fetchall()
    now = datetime.now()
    for r in rows:
        exp  = datetime.fromisoformat(r["expires_at"])
        dias = (exp - now).days
        try:
            if dias == 3:
                await bot.send_message(
                    r["chat_id"],
                    "⚠️ La licencia vence en <b>3 días</b>. Contactá al admin para renovarla.",
                    parse_mode="HTML"
                )
            elif dias == 1:
                await bot.send_message(
                    r["chat_id"], "🚨 La licencia vence <b>mañana</b>. ¡Renovála hoy!", parse_mode="HTML"
                )
            elif now > exp + timedelta(hours=24):
                with get_db() as conn2:
                    conn2.execute(
                        "UPDATE licenses SET is_active=0 WHERE chat_id=?", (r["chat_id"],)
                    )
                await bot.send_message(
                    r["chat_id"],
                    "⛔ La licencia venció y el período de gracia terminó. El bot fue <b>desactivado</b>.",
                    parse_mode="HTML"
                )
        except Exception as e:
            logger.error(f"Error aviso vencimiento {r['chat_id']}: {e}")

# ─── Ayuda ────────────────────────────────────────────────────────────────────

AYUDA = (
    "🤖 <b>Bot Gestor de Grupos</b>\n\n"
    "⚙️ /menu — Panel de configuración\n\n"
    "🔑 /activar &lt;código&gt;\n\n"
    "⚔️ <b>Moderación</b>\n"
    "/kick · /ban · /unban · /mute [min] · /unmute\n\n"
    "🚫 <b>Palabras</b>\n"
    "/addword · /addwordglobal · /removeword · /listwords\n\n"
    "📢 <b>Anuncios</b>\n"
    "/announce · /listanuncios · /veranuncio &lt;id&gt; · /delanuncio &lt;id&gt;\n\n"
    "👥 <b>Grupos (admin del bot)</b>\n"
    "/mainconnect &lt;id&gt; — Establecer líder\n"
    "/subconnect &lt;id&gt; — Registrar sub-grupo\n\n"
    "🌍 Compartí tu <b>ubicación</b> en el grupo para configurar la zona horaria.\n\n"
    "⚠️ 3 advertencias = mute automático."
)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(AYUDA, parse_mode="HTML")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    init_db()

    if ADMIN_ID == 0:
        logger.warning("⚠️  ADMIN_ID no configurado.")

    app = Application.builder().token(BOT_TOKEN).build()

    # ConversationHandler: crear anuncio
    conv_announce = ConversationHandler(
        entry_points=[
            CommandHandler("announce", cmd_announce),
            CallbackQueryHandler(cb_ann_create_start, pattern="^ann:create$"),
        ],
        states={
            ANN_TEXT:      [MessageHandler(filters.TEXT & ~filters.COMMAND, ann_texto)],
            ANN_MEDIA:     [
                MessageHandler(filters.PHOTO | filters.VIDEO, ann_media),
                CommandHandler("skip", ann_media),
            ],
            ANN_BUTTONS:   [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ann_botones),
                CommandHandler("skip", ann_botones),
            ],
            ANN_INTERVAL:  [MessageHandler(filters.TEXT & ~filters.COMMAND, ann_intervalo)],
            ANN_STARTTIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ann_starttime),
                CommandHandler("skip", ann_starttime),
            ],
        },
        fallbacks=[CommandHandler("cancelar", ann_cancelar)],
        allow_reentry=True,
    )

    # ConversationHandler: editar anuncio
    conv_edit_ann = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_ann_edit_start, pattern="^ann:edit$")],
        states={
            ANN_EDIT_ID:    [MessageHandler(filters.TEXT & ~filters.COMMAND, ann_edit_get_id)],
            ANN_EDIT_FIELD: [MessageHandler(filters.TEXT & ~filters.COMMAND, ann_edit_get_field)],
            ANN_EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ann_edit_save)],
        },
        fallbacks=[
            CommandHandler("cancelar", conv_cancelar),
            CallbackQueryHandler(cb_navigation, pattern="^menu:"),
        ],
        allow_reentry=True,
    )

    # ConversationHandler: inputs desde el menú
    conv_menu_inputs = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_word_add_start,    pattern="^words:add_"),
            CallbackQueryHandler(cb_word_del_start,    pattern="^words:delete$"),
            CallbackQueryHandler(cb_ann_preview_start, pattern="^ann:preview$"),
            CallbackQueryHandler(cb_ann_del_start,     pattern="^ann:delete$"),
            CallbackQueryHandler(cb_sub_del_start,     pattern="^groups:delete$"),
        ],
        states={
            WORD_ADD_WAIT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, cb_word_add_finish)],
            WORD_DEL_WAIT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, cb_word_del_finish)],
            ANN_PREVIEW_WAIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, cb_ann_preview_finish)],
            ANN_DEL_WAIT:     [MessageHandler(filters.TEXT & ~filters.COMMAND, cb_ann_del_finish)],
            SUB_DEL_WAIT:     [MessageHandler(filters.TEXT & ~filters.COMMAND, cb_sub_del_finish)],
        },
        fallbacks=[
            CommandHandler("cancelar", conv_cancelar),
            CallbackQueryHandler(cb_navigation, pattern="^menu:"),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv_announce)
    app.add_handler(conv_edit_ann)
    app.add_handler(conv_menu_inputs)
    app.add_handler(CallbackQueryHandler(cb_navigation))

    app.add_handler(CommandHandler("start",         cmd_help))
    app.add_handler(CommandHandler("help",          cmd_help))
    app.add_handler(CommandHandler("menu",          cmd_menu))
    app.add_handler(CommandHandler("activar",       cmd_activar))

    app.add_handler(CommandHandler("generar",       cmd_generar))
    app.add_handler(CommandHandler("revocar",       cmd_revocar))
    app.add_handler(CommandHandler("licencias",     cmd_licencias))
    app.add_handler(CommandHandler("mainconnect",   cmd_mainconnect))
    app.add_handler(CommandHandler("subconnect",    cmd_subconnect))

    app.add_handler(CommandHandler("kick",          cmd_kick))
    app.add_handler(CommandHandler("ban",           cmd_ban))
    app.add_handler(CommandHandler("unban",         cmd_unban))
    app.add_handler(CommandHandler("mute",          cmd_mute))
    app.add_handler(CommandHandler("unmute",        cmd_unmute))

    app.add_handler(CommandHandler("addword",       cmd_addword))
    app.add_handler(CommandHandler("addwordglobal", cmd_addwordglobal))
    app.add_handler(CommandHandler("removeword",    cmd_removeword))
    app.add_handler(CommandHandler("listwords",     cmd_listwords))

    app.add_handler(CommandHandler("listanuncios",  cmd_listanuncios))
    app.add_handler(CommandHandler("delanuncio",    cmd_delanuncio))
    app.add_handler(CommandHandler("veranuncio",    cmd_veranuncio))

    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND,
        filtrar_mensajes
    ))

    # Recargar anuncios al iniciar
    with get_db() as conn:
        anuncios = conn.execute(
            "SELECT id, chat_id, text, media_file_id, media_type, buttons_json, interval_hours, start_time "
            "FROM announcements"
        ).fetchall()
    for a in anuncios:
        _programar_anuncio(
            app, a["id"], a["chat_id"], a["text"],
            a["media_file_id"], a["media_type"], a["buttons_json"],
            a["interval_hours"], a["start_time"], a["chat_id"]
        )

    scheduler.add_job(
        job_avisar_vencimientos,
        CronTrigger(hour=10, minute=0),
        args=[app.bot],
        id="check_licenses",
        replace_existing=True,
    )

    scheduler.start()
    logger.info("✅ Bot iniciado correctamente.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
