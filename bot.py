import os
import re
import sys
import ast
import json
import shutil
import signal
import shlex
import asyncio
import logging
import zipfile
import functools
import importlib.util
from datetime import datetime, timedelta

import psutil
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.client.default import DefaultBotProperties

# ----------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------
# These are used directly. If a BOT_TOKEN / ADMIN_ID Environment Variable is
# also set on Railway from an older setup, it is ignored — whatever is
# written here always wins, so editing this file is always enough.
DEFAULT_BOT_TOKEN = "8698303828:AAFecdZ6rUhQD_hvhMA4dWDm5Oop6_3j9Wc"
DEFAULT_ADMIN_ID = 8904843897

# SECURITY: environment variables now take priority over the hardcoded
# defaults (kept as a fallback so an existing Railway deploy keeps working).
BOT_TOKEN = (os.getenv("BOT_TOKEN") or DEFAULT_BOT_TOKEN or "").strip()
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID") or str(DEFAULT_ADMIN_ID) or "0")
except ValueError:
    ADMIN_ID = 0

DB_FILE = "running_db.json"
USERS_DB_FILE = "users_db.json"
ADMINS_FILE = "admins_db.json"
WEBHOOKS_DB_FILE = "webhooks_db.json"
MAX_USER_PROJECTS = 3

# Intentional pre-run delay (seconds) with friendly emoji progress messages.
RUN_DELAY_SECONDS = 30

# Zip-bomb guard: reject archives whose total uncompressed size exceeds this.
MAX_ZIP_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("runner")

if not BOT_TOKEN:
    logger.critical(
        "BOT_TOKEN is not set. Add it in Railway's Environment Variables as "
        "BOT_TOKEN and redeploy the service."
    )
    sys.exit(1)

# SECURITY: warn early if the token format looks wrong (warn only, never crash).
if not re.fullmatch(r"[0-9]{6,12}:[A-Za-z0-9_\-]{30,70}", BOT_TOKEN):
    logger.warning("BOT_TOKEN does not look like a valid Telegram bot token — check BotFather.")

if not ADMIN_ID:
    logger.critical(
        "ADMIN_ID is not set or invalid. Add it in Railway's Environment Variables as "
        "ADMIN_ID (your numeric Telegram user ID)."
    )
    sys.exit(1)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="Markdown"))
dp = Dispatcher(storage=MemoryStorage())

RUNNING_PROCESSES = {}

# Every user who has ever pressed /start, keyed by str(user_id):
#   {"username": str, "first_name": str, "lang": "fa"|"en", "banned": bool, "joined": str}
USERS_DB = {}

# In-memory cache of each user's chosen language, keyed by int user id.
# Rebuilt from USERS_DB on startup so language survives restarts.
USER_LANG = {}

# Set of admin user ids. Always contains ADMIN_ID; more can be added from the admin panel.
ADMINS = {ADMIN_ID}

# Registered webhooks, keyed by str(pid).
WEBHOOKS_DB = {}


# ----------------------------------------------------
# TRANSLATIONS
# ----------------------------------------------------
TEXTS = {
    # menu buttons
    "upload_run": {"fa": "📤 آپلود و اجرای پروژه", "en": "📤 Upload & Run Project"},
    "my_projects": {"fa": "📁 پروژه‌های من", "en": "📁 My Projects"},
    "server_status_btn": {"fa": "🖥 وضعیت سرور", "en": "🖥 Server Status"},
    "tutorial_btn": {"fa": "📖 آموزش", "en": "📖 Tutorial"},
    "cancel_btn": {"fa": "❌ لغو", "en": "❌ Cancel"},
    "admin_panel_btn": {"fa": "🛠 پنل مدیریت", "en": "🛠 Admin Panel"},
    "hours_btn": {"fa": "⏱ ساعت", "en": "⏱ Hours"},
    "days_btn": {"fa": "📅 روز", "en": "📅 Days"},
    "stop_delete_btn": {"fa": "🛑 توقف و حذف", "en": "🛑 Stop & Delete"},
    "extend_time_btn": {"fa": "➕ افزایش زمان", "en": "➕ Extend Time"},
    "back_btn": {"fa": "🔙 بازگشت", "en": "🔙 Back"},

    # admin panel buttons
    "search_user_btn": {"fa": "🔍 جستجوی کاربر", "en": "🔍 Search User"},
    "ban_user_btn": {"fa": "🚫 بن کردن کاربر", "en": "🚫 Ban User"},
    "unban_user_btn": {"fa": "✅ آن‌بن کردن کاربر", "en": "✅ Unban User"},
    "broadcast_btn": {"fa": "📢 پیام همگانی", "en": "📢 Broadcast Message"},
    "server_info_admin_btn": {"fa": "🖥 مشخصات سرور", "en": "🖥 Server Info"},
    "all_sources_info_btn": {"fa": "📋 اطلاعات سورس‌های در حال اجرا", "en": "📋 All Running Sources Info"},
    "clear_all_sources_btn": {"fa": "🧹 پاکسازی همه سورس‌ها", "en": "🧹 Clear All Sources"},
    "add_admin_btn": {"fa": "👑 اضافه کردن ادمین", "en": "👑 Add Admin"},

    # general messages
    "welcome": {
        "fa": "👋 **خوش اومدی!**\n\nربات اجرای کد شما آنلاین و آماده‌ست. 🚀\nپروژه‌ت رو آپلود کن و اجراش کن.",
        "en": "👋 **Welcome!**\n\nYour code runner bot is online and ready. 🚀\nUpload your project and run it.",
    },
    "operation_canceled": {"fa": "❌ عملیات لغو شد.", "en": "❌ Operation canceled."},
    "banned_message": {
        "fa": "🚫 شما توسط مدیریت مسدود شده‌اید و امکان استفاده از ربات را ندارید.",
        "en": "🚫 You have been banned from using this bot.",
    },
    "unexpected_error": {
        "fa": "⚠️ یک خطای غیرمنتظره رخ داد، اما ربات کرش نکرد:\n`{err}`",
        "en": "⚠️ An unexpected error occurred, but the bot did not crash:\n`{err}`",
    },
    "fallback_unrecognized": {
        "fa": "🤔 متوجه این پیام نشدم. روی /start بزن یا از دکمه‌های منو استفاده کن.",
        "en": "🤔 I didn't recognize that. Tap /start or use the menu buttons below.",
    },

    "tutorial_text": {
        "fa": (
            "📚 **آموزش ربات اجرای کد**\n\n"
            "• **زبان‌های پشتیبانی‌شده:** پایتون (`.py`)، Node.js (`.js`)، TypeScript (`.ts`)، "
            "Go (`.go`)، Java (`.java`)، PHP (`.php`)، Rust (`.rs`)\n"
            "• **فایل‌های ZIP:** برای پروژه‌های چندفایلی، فایل `.zip` آپلود کن — ربات به‌صورت "
            "خودکار فایل اصلی اجرا رو پیدا می‌کنه.\n"
            "• **نصب خودکار کتابخانه‌ها:** پکیج‌های پایتون/Node.js که در سورس استفاده شدن ولی "
            "نصب نیستن، حتی بدون `requirements.txt` یا `package.json` به‌صورت خودکار شناسایی و "
            "نصب می‌شن.\n"
            "• **نصب خودکار Runtime:** پایتون همیشه آماده‌ست. برای Node/Go/Java/PHP/Rust، ربات "
            "runtime لازم رو در اولین استفاده نصب می‌کنه — ممکنه بار اول کمی طول بکشه."
        ),
        "en": (
            "📚 **Code Runner Tutorial**\n\n"
            "• **Supported Languages:** Python (`.py`), Node.js (`.js`), TypeScript (`.ts`), "
            "Go (`.go`), Java (`.java`), PHP (`.php`), Rust (`.rs`)\n"
            "• **ZIP Packages:** Upload `.zip` files for multi-file projects — the bot "
            "automatically finds the right entry file.\n"
            "• **Auto Libraries:** Missing Python/Node packages are detected from your "
            "source code and installed automatically, even without a `requirements.txt` "
            "or `package.json`.\n"
            "• **Auto Runtimes:** Python is always ready to go. For Node/Go/Java/PHP/Rust, "
            "the bot installs the required runtime the first time it's needed — this can "
            "add a short delay on first use of a new language."
        ),
    },

    "send_file_prompt": {"fa": "📥 فایل پروژه یا آرشیو ZIP رو بفرست:", "en": "📥 Send your project file or ZIP archive:"},
    "unsupported_ext": {"fa": "❌ پسوند فایل پشتیبانی نمی‌شه!", "en": "❌ Unsupported file extension!"},
    "max_projects_reached": {
        "fa": "❌ به سقف پروژه‌های فعال رسیدی ({max}). اول یکی از پروژه‌ها رو متوقف کن.",
        "en": "❌ Maximum active projects reached ({max}). Stop a project first.",
    },
    "download_failed": {"fa": "❌ دانلود فایل با خطا مواجه شد: `{err}`", "en": "❌ File download failed: `{err}`"},
    "zip_extract_failed": {"fa": "❌ استخراج فایل ZIP با خطا مواجه شد: `{err}`", "en": "❌ Zip extraction failed: `{err}`"},
    "project_type_undetected": {
        "fa": "❌ نوع پروژه داخل ZIP تشخیص داده نشد.",
        "en": "❌ Could not detect the project type inside the ZIP.",
    },
    "multiple_entry_files": {
        "fa": "📂 چند فایل قابل‌اجرا داخل ZIP پیدا شد. کدوم رو اجرا کنم؟",
        "en": "📂 Multiple runnable files were found in the ZIP. Which one should I run?",
    },
    "no_runnable_file": {
        "fa": "❌ هیچ فایل قابل‌اجرایی داخل ZIP پیدا نشد.",
        "en": "❌ No runnable file was found inside the ZIP.",
    },
    "choose_shown_option": {"fa": "❌ لطفاً یکی از گزینه‌های نمایش‌داده‌شده رو انتخاب کن.", "en": "❌ Please choose one of the options shown."},
    "select_runtime_unit": {"fa": "⏰ واحد زمان اجرا رو انتخاب کن:", "en": "⏰ Select runtime unit:"},
    "enter_duration": {"fa": "⌛ مدت زمان اجرا رو به **{unit}** وارد کن:", "en": "⌛ Enter runtime duration in **{unit}**:"},
    "invalid_positive_integer": {"fa": "❌ یک عدد صحیح مثبت معتبر بفرست.", "en": "❌ Send a valid positive integer."},
    "installing_packages": {"fa": "⚙️ در حال نصب پکیج‌ها و آماده‌سازی runtime...", "en": "⚙️ Installing packages and preparing runtime..."},

    "runtime_installing": {
        "fa": "⚙️ runtime مربوط به {type} روی این کانتینر نصب نیست — در حال تلاش برای نصب خودکار...",
        "en": "⚙️ The {type} runtime isn't installed in this container yet — attempting to install it automatically...",
    },
    "runtime_installed_success": {"fa": "✅ runtime مربوط به {type} با موفقیت نصب شد.", "en": "✅ {type} runtime installed successfully."},
    "runtime_install_failed": {
        "fa": "❌ نصب خودکار runtime مربوط به {type} روی این کانتینر ممکن نشد (شاید اجازه نصب پکیج داده نشده). "
              "پروژه‌های پایتون بدون مشکل اجرا می‌شن.",
        "en": "❌ Couldn't install the {type} runtime automatically in this container "
              "(it may not allow package installation). Python projects are unaffected and will "
              "still run normally.",
    },
    "detected_py_packages": {
        "fa": "🔎 پکیج‌های پایتونی که در سورس استفاده شدن ولی نصب نیستن:\n`{pkgs}`\nدر حال نصب خودکار...",
        "en": "🔎 Detected Python packages used in the source but not installed:\n`{pkgs}`\nInstalling automatically...",
    },
    "req_install_failed": {"fa": "⚠️ نصب requirements.txt با خطا مواجه شد:\n`{out}`", "en": "⚠️ Installing requirements.txt failed:\n`{out}`"},
    "detected_py_install_failed": {
        "fa": "⚠️ نصب برخی پکیج‌های شناسایی‌شده با خطا مواجه شد:\n`{out}`",
        "en": "⚠️ Installing some detected packages failed:\n`{out}`",
    },
    "npm_install_failed": {"fa": "⚠️ npm install با خطا مواجه شد:\n`{out}`", "en": "⚠️ npm install failed:\n`{out}`"},
    "detected_node_packages": {
        "fa": "🔎 پکیج‌های Node.js که در سورس استفاده شدن ولی نصب نیستن:\n`{pkgs}`\nدر حال نصب خودکار...",
        "en": "🔎 Detected Node.js packages used in the source but not installed:\n`{pkgs}`\nInstalling automatically...",
    },
    "detected_node_install_failed": {
        "fa": "⚠️ نصب برخی پکیج‌های Node.js شناسایی‌شده با خطا مواجه شد:\n`{out}`",
        "en": "⚠️ Installing some detected Node.js packages failed:\n`{out}`",
    },
    "runtime_env_unsupported": {
        "fa": "❌ اجرای این نوع پروژه در محیط فعلی ممکن نشد.",
        "en": "❌ Could not run this project type in the current environment.",
    },
    "project_type_unsupported": {"fa": "❌ این نوع پروژه پشتیبانی نمی‌شه.", "en": "❌ This project type is not supported."},

    "project_running": {
"fa": "🚀 **پروژه در حال اجراست!**\n\n📄 **نام:** `{name}`\n🆔 **PID:** `{pid}`\n📅 **انقضا:** `{end}`",

        "en": "🚀 **Project is now Running!**\n\n📄 **Name:** `{name}`\n🆔🆔 **PID:** `{pid}`\n📅 **Expires At:** `{end}`",
    },
    "execution_error": {"fa": "❌ خطای اجرا: `{err}`", "en": "❌ Execution Error: `{err}`"},

    "no_active_projects": {"fa": "ℹ️ در حال حاضر هیچ پروژه‌ی فعالی در حال اجرا نیست.", "en": "ℹ️ No active projects currently running."},
    "active_projects_header": {"fa": "📁 **پروژه‌های فعال:**\n\n", "en": "📁 **Active Projects:**\n\n"},
    "remaining_label": {"fa": "باقی‌مانده", "en": "Remaining"},
    "manage_pid_prefix": {"fa": "⚙️ مدیریت پروژه ", "en": "⚙️ Manage PID "},
    "action_for_pid": {"fa": "⚙️ عملیات برای PID `{pid}`:", "en": "⚙️ Action for PID `{pid}`:"},
    "pid_stopped": {"fa": "✅ پروژه با PID `{pid}` متوقف شد.", "en": "✅ PID `{pid}` stopped."},
    "enter_extra_hours": {"fa": "⌛ چند ساعت اضافه بشه رو وارد کن:", "en": "⌛ Enter extra hours to add:"},
    "invalid_number": {"fa": "❌ یک عدد معتبر وارد کن.", "en": "❌ Enter a valid number."},
    "extended_success": {"fa": "✅ زمان تمدید شد! انقضای جدید: `{end}`", "en": "✅ Extended! Expiry: `{end}`"},

    "server_status_text": {
        "fa": (
            "🖥 **وضعیت جزئی سرور**\n\n"
            "💻 **مصرف CPU:** `{cpu}%`\n"
            "🧠 **RAM مصرف‌شده:** `{ram_used} MB` / `{ram_total} MB` (`{ram_percent}%`)\n"
            "🧠 **RAM آزاد:** `{ram_free} MB`\n"
            "💾 **فضای آزاد دیسک:** `{disk_free} GB`\n"
            "⚙️ **پروژه‌های فعال:** `{active}`"
        ),
        "en": (
            "🖥 **Detailed Server Status**\n\n"
            "💻 **CPU Usage:** `{cpu}%`\n"
            "🧠 **RAM Used:** `{ram_used} MB` / `{ram_total} MB` (`{ram_percent}%`)\n"
            "🧠 **RAM Free:** `{ram_free} MB`\n"
            "💾 **Disk Free:** `{disk_free} GB`\n"
            "⚙️ **Active Projects:** `{active}`"
        ),
    },

    # admin panel
    "admin_panel_title": {"fa": "🛠 **پنل مدیریت**\n\nیکی از گزینه‌ها رو انتخاب کن:", "en": "🛠 **Admin Panel**\n\nChoose an option:"},
    "not_admin_access": {"fa": "⛔ شما دسترسی ادمین ندارید.", "en": "⛔ You don't have admin access."},
    "main_menu": {"fa": "🏠 منوی اصلی", "en": "🏠 Main Menu"},

    "ask_user_id_search": {"fa": "🔍 آیدی عددی کاربر مورد نظر رو وارد کن:", "en": "🔍 Enter the user's numeric ID:"},
    "invalid_id": {"fa": "❌ آیدی عددی نامعتبره. یک عدد بفرست.", "en": "❌ Invalid numeric ID. Send a number."},
    "user_not_found": {
        "fa": "❌ این کاربر ربات رو استارت نکرده (آیدی توی دیتابیس پیدا نشد).",
        "en": "❌ This user has not started the bot (ID not found in the database).",
    },

    "ask_ban_id": {"fa": "🚫 آیدی عددی کاربری که می‌خوای بن کنی رو وارد کن:", "en": "🚫 Enter the numeric ID of the user to ban:"},
    "user_banned": {"fa": "✅ کاربر `{id}` بن شد.", "en": "✅ User `{id}` has been banned."},
    "already_banned": {"fa": "⚠️ این کاربر از قبل بن شده.", "en": "⚠️ This user is already banned."},

    "ask_unban_id": {"fa": "✅ آیدی عددی کاربری که می‌خوای آن‌بن کنی رو وارد کن:", "en": "✅ Enter the numeric ID of the user to unban:"},
    "user_unbanned": {"fa": "✅ کاربر `{id}` آن‌بن شد.", "en": "✅ User `{id}` has been unbanned."},
    "not_banned": {"fa": "⚠️ این کاربر بن نیست.", "en": "⚠️ This user is not banned."},

    "ask_broadcast_text": {"fa": "📢 متن پیام همگانی رو ارسال کن:", "en": "📢 Send the broadcast message text:"},
    "broadcast_done": {"fa": "✅ پیام همگانی برای {count} کاربر ارسال شد. ({failed} ناموفق)", "en": "✅ Broadcast sent to {count} users. ({failed} failed)"},

    "no_sources_running": {"fa": "ℹ️ در حال حاضر هیچ سورسی در حال اجرا نیست.", "en": "ℹ️ No sources are currently running."},
    "all_sources_header": {"fa": "📋 **همه‌ی سورس‌های در حال اجرا:**\n\n", "en": "📋 **All Running Sources:**\n\n"},
    "clear_all_done": {"fa": "✅ همه‌ی سورس‌های در حال اجرا پاکسازی شدند. ({count} مورد)", "en": "✅ All running sources were cleared. ({count} items)"},

    "ask_add_admin_id": {"fa": "👑 آیدی عددی کاربری که می‌خوای ادمین کنی رو وارد کن:", "en": "👑 Enter the numeric ID of the user to make admin:"},
    "admin_added": {"fa": "✅ کاربر `{id}` به عنوان ادمین اضافه شد.", "en": "✅ User `{id}` has been added as admin."},
    "already_admin": {"fa": "⚠️ این کاربر از قبل ادمین است.", "en": "⚠️ This user is already an admin."},

    # progress / webhook additions
    "webhook_ready": {
        "fa": "🔗 وبهوک پروژه به‌صورت خودکار تنظیم شد (برای PHP حتی بدون هیچ کانفیگی — توکن و آدرس از خود پروژه پیدا می‌شه) و هنگام شروع صدا زده شد!",
        "en": "🔗 Project webhook was set up automatically (for PHP even with zero config - token & URL are discovered from the project itself) and pinged on start!",
    },
}


def t(key, lang):
    entry = TEXTS.get(key, {})
    return entry.get(lang, entry.get("en", key))


def get_lang(user_id):
    return USER_LANG.get(user_id, "en")


def is_admin(user_id):
    return user_id in ADMINS


def is_banned(user_id):
    return USERS_DB.get(str(user_id), {}).get("banned", False)


# ----------------------------------------------------
# DATABASE HELPERS
# ----------------------------------------------------
def save_db():
    try:
        data = {}
        for pid, info in RUNNING_PROCESSES.items():
            data[str(pid)] = {
                "file_name": info["file_name"],
                "work_dir": info["work_dir"],
                "user_id": info["user_id"],
                "end_time": info["end_time"].strftime("%Y-%m-%d %H:%M:%S"),
            }
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception:
        logger.exception("save_db failed")


def cleanup_stale_state():
    """On (re)start, any in-memory process handles from a previous run are gone.
    Reset the on-disk db and wipe leftover work directories so old, dead
    entries never confuse the bot or fill up the disk."""
    try:
        if os.path.exists(DB_FILE):
            os.remove(DB_FILE)
    except Exception:
        logger.exception("could not remove stale db file")

    try:
        for entry in os.listdir(os.getcwd()):
            if entry.startswith("run_") and os.path.isdir(entry):
                shutil.rmtree(entry, ignore_errors=True)
    except Exception:
        logger.exception("could not clean stale run_ directories")


def load_users_db():
    global USERS_DB
    try:
        if os.path.exists(USERS_DB_FILE):
            with open(USERS_DB_FILE, "r", encoding="utf-8") as f:
                USERS_DB = json.load(f)
    except Exception:
        logger.exception("load_users_db failed")
        USERS_DB = {}

    # restore each known user's language into the runtime cache
    for uid, info in USERS_DB.items():
        try:
            USER_LANG[int(uid)] = info.get("lang", "en")
        except Exception:
            continue


def save_users_db():
    try:
        with open(USERS_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(USERS_DB, f, ensure_ascii=False, indent=4)
    except Exception:
        logger.exception("save_users_db failed")


def load_admins():
    global ADMINS
    ADMINS = {ADMIN_ID}
    try:
        if os.path.exists(ADMINS_FILE):
            with open(ADMINS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            ADMINS |= {int(x) for x in data}
    except Exception:
        logger.exception("load_admins failed")


def save_admins():
    try:
        with open(ADMINS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(ADMINS), f)
    except Exception:
        logger.exception("save_admins failed")


def load_webhooks_db():
    global WEBHOOKS_DB
    try:
        if os.path.exists(WEBHOOKS_DB_FILE):
            with open(WEBHOOKS_DB_FILE, "r", encoding="utf-8") as f:
                WEBHOOKS_DB = json.load(f)
    except Exception:
        logger.exception("load_webhooks_db failed")
        WEBHOOKS_DB = {}


def save_webhooks_db():
    try:
        with open(WEBHOOKS_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(WEBHOOKS_DB, f, ensure_ascii=False, indent=4)
    except Exception:
        logger.exception("save_webhooks_db failed")


def register_user(user: types.User):
    """Remember every user who has ever started the bot, so the admin panel
    can look them up, ban/unban them, and broadcast to them by numeric ID."""
    uid = str(user.id)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if uid not in USERS_DB:
        USERS_DB[uid] = {
            "username": user.username or "",
            "first_name": user.first_name or "",
            "lang": USER_LANG.get(user.id, "en"),
            "banned": False,
            "joined": now,
        }
    else:
        USERS_DB[uid]["username"] = user.username or USERS_DB[uid].get("username", "")
        USERS_DB[uid]["first_name"] = user.first_name or USERS_DB[uid].get("first_name", "")
        USER_LANG[user.id] = USERS_DB[uid].get("lang", "en")
    save_users_db()


# ----------------------------------------------------
# STATES
# ----------------------------------------------------
class LanguageState(StatesGroup):
    waiting_for_language = State()


class UploadState(StatesGroup):
    waiting_for_file = State()
    waiting_for_entry_choice = State()
    waiting_for_duration_unit = State()
    waiting_for_duration_value = State()


class ProjectManageState(StatesGroup):
    waiting_for_project_selection = State()
    waiting_for_action = State()
    waiting_for_extend_time = State()


class AdminState(StatesGroup):
    waiting_for_search_id = State()
    waiting_for_ban_id = State()
    waiting_for_unban_id = State()
    waiting_for_broadcast_text = State()
    waiting_for_add_admin_id = State()


# ----------------------------------------------------
# KEYBOARDS
# ----------------------------------------------------
def kbtn(text, style=None):
    """Colored keyboard button (Bot API 9.4 'style' field, needs aiogram>=3.20).
    Falls back to a plain button automatically if the installed aiogram/Bot API
    combo doesn't support styling, so this can never raise an error."""
    if style:
        try:
            return KeyboardButton(text=text, style=style)
        except Exception:
            logger.debug("Colored button style unsupported, falling back to plain button")
    return KeyboardButton(text=text)


def get_language_keyboard():
    buttons = [
        [kbtn("🇮🇷 فارسی", "primary"), kbtn("🇬🇧 English", "primary")],
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def get_main_keyboard(lang, admin=False):
    buttons = [
        [kbtn(t("upload_run", lang), "primary")],
        [kbtn(t("my_projects", lang), "primary"), kbtn(t("server_status_btn", lang), "primary")],
        [kbtn(t("tutorial_btn", lang), "success")],
    ]
    if admin:
        buttons.append([kbtn(t("admin_panel_btn", lang), "danger")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def get_cancel_keyboard(lang):
    return ReplyKeyboardMarkup(
        keyboard=[[kbtn(t("cancel_btn", lang), "danger")]],
        resize_keyboard=True,
    )


def get_time_unit_keyboard(lang):
    return ReplyKeyboardMarkup(
        keyboard=[
            [kbtn(t("hours_btn", lang), "primary"), kbtn(t("days_btn", lang), "success")],
            [kbtn(t("cancel_btn", lang), "danger")],
        ],
        resize_keyboard=True,
    )


def get_manage_action_keyboard(lang):
    return ReplyKeyboardMarkup(
        keyboard=[
            [kbtn(t("stop_delete_btn", lang), "danger")],
            [kbtn(t("extend_time_btn", lang), "success")],
            [kbtn(t("cancel_btn", lang), "primary")],
        ],
        resize_keyboard=True,
    )


def get_admin_keyboard(lang):
    buttons = [
        [kbtn(t("search_user_btn", lang), "primary"), kbtn(t("ban_user_btn", lang), "danger")],
        [kbtn(t("unban_user_btn", lang), "success"), kbtn(t("broadcast_btn", lang), "primary")],
        [kbtn(t("server_info_admin_btn", lang), "success"), kbtn(t("all_sources_info_btn", lang), "primary")],
        [kbtn(t("clear_all_sources_btn", lang), "danger"), kbtn(t("add_admin_btn", lang), "success")],
        [kbtn(t("back_btn", lang), "danger")],
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


# Public access enabled. Admin checks are handled only in admin-only features.


def safe_handler(func):
    """Wrap every handler so that no exception can ever crash the bot or
    leave the user without a response."""

    @functools.wraps(func)
    async def wrapper(message: types.Message, *args, **kwargs):
        try:
            return await func(message, *args, **kwargs)
        except Exception as e:
            logger.exception("handler '%s' failed", func.__name__)
            try:
                lang = get_lang(message.from_user.id) if message.from_user else "en"
                await message.answer(
                    t("unexpected_error", lang).format(err=str(e)[:300]),
                    reply_markup=get_main_keyboard(lang, is_admin(message.from_user.id) if message.from_user else False),
                )
            except Exception:
                pass

    return wrapper


@dp.error()
async def global_error_handler(event: types.ErrorEvent):
    logger.exception("Unhandled error while processing update", exc_info=event.exception)
    try:
        await bot.send_message(
            ADMIN_ID,
            f"⚠️ Handled internal error:\n`{str(event.exception)[:500]}`",
        )
    except Exception:
        pass
    return True


# ----------------------------------------------------
# BAN MIDDLEWARE (blocks banned users from every handler below)
# ----------------------------------------------------
@dp.message.middleware()
async def ban_guard_middleware(handler, event: types.Message, data):
    user = event.from_user
    if user and is_banned(user.id):
        try:
            await event.answer(t("banned_message", get_lang(user.id)))
        except Exception:
            pass
        return
    return await handler(event, data)


# ----------------------------------------------------
# COMMANDS & HANDLERS
# ----------------------------------------------------
@dp.message(CommandStart())
@safe_handler
async def cmd_start(message: types.Message, state: FSMContext):
    logger.info("USER START | id=%s | username=%s", message.from_user.id, message.from_user.username)
    register_user(message.from_user)
    await state.clear()
    await state.set_state(LanguageState.waiting_for_language)
    await message.answer(
        "🌐 **Please Select Language / لطفا زبان خود را انتخاب کنید**\n\n"
        "🇮🇷 فارسی    |    🇬🇧 English",
        reply_markup=get_language_keyboard(),
    )


@dp.message(LanguageState.waiting_for_language, F.text.in_(["🇮🇷 فارسی", "🇬🇧 English"]))
@safe_handler
async def language_selected(message: types.Message, state: FSMContext):
    lang = "fa" if "فارسی" in message.text else "en"
    USER_LANG[message.from_user.id] = lang
    uid = str(message.from_user.id)
    if uid in USERS_DB:
        USERS_DB[uid]["lang"] = lang
        save_users_db()
    logger.info("LANGUAGE SET | id=%s | lang=%s", message.from_user.id, lang)
    await state.clear()
    await message.answer(
        t("welcome", lang),
        reply_markup=get_main_keyboard(lang, is_admin(message.from_user.id)),
    )


@dp.message(F.text.in_([t("cancel_btn", "fa"), t("cancel_btn", "en")]))
@safe_handler
async def cancel_action(message: types.Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    await state.clear()
    await message.answer(t("operation_canceled", lang), reply_markup=get_main_keyboard(lang, is_admin(message.from_user.id)))


@dp.message(F.text.in_([t("tutorial_btn", "fa"), t("tutorial_btn", "en")]))
@safe_handler
async def show_tutorial(message: types.Message):
    lang = get_lang(message.from_user.id)
    await message.answer(t("tutorial_text", lang))


def build_server_status_text(lang):
    cpu = psutil.cpu_percent(interval=1)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    return t("server_status_text", lang).format(
        cpu=cpu,
        ram_used=ram.used // (1024 ** 2),
        ram_total=ram.total // (1024 ** 2),
        ram_percent=ram.percent,
        ram_free=ram.available // (1024 ** 2),
        disk_free=disk.free // (1024 ** 3),
        active=len(RUNNING_PROCESSES),
    )


@dp.message(F.text.in_([t("server_status_btn", "fa"), t("server_status_btn", "en")]))
@safe_handler
async def server_status(message: types.Message):
    lang = get_lang(message.from_user.id)
    await message.answer(build_server_status_text(lang))


# ----------------------------------------------------
# PROJECT / DEPENDENCY ANALYSIS HELPERS
# ----------------------------------------------------
IMPORT_TO_PIP = {
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "yaml": "PyYAML",
    "bs4": "beautifulsoup4",
    "dotenv": "python-dotenv",
    "Crypto": "pycryptodome",
    "Cryptodome": "pycryptodome",
    "sklearn": "scikit-learn",
    "telebot": "pyTelegramBotAPI",
    "telegram": "python-telegram-bot",
    "jwt": "PyJWT",
    "MySQLdb": "mysqlclient",
    "psycopg2": "psycopg2-binary",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "fitz": "PyMuPDF",
    "socks": "PySocks",
    "OpenSSL": "pyOpenSSL",
    "serial": "pyserial",
    "win32api": "pywin32",
    "Xlib": "python-xlib",
    "telethon": "Telethon",
    "pyrogram": "pyrogram",
    "tgcrypto": "TgCrypto",
}

try:
    STDLIB_MODULES = set(sys.stdlib_module_names)
except AttributeError:
    STDLIB_MODULES = set(sys.builtin_module_names)

IGNORED_DIR_NAMES = {"__pycache__", "__MACOSX", ".git", "node_modules", "venv", ".venv"}

# entry-file candidates and recognized extensions per project type
ENTRY_CONFIG = {
    "python": (["main.py", "bot.py", "run.py", "app.py", "start.py", "runner.py"], (".py",)),
    "node": (
        ["index.js", "main.js", "app.js", "server.js", "bot.js",
         "index.ts", "main.ts", "app.ts", "server.ts", "bot.ts"],
        (".js", ".ts"),
    ),
    "go": (["main.go", "bot.go", "app.go"], (".go",)),
    "java": (["Main.java", "App.java", "Bot.java", "Runner.java"], (".java",)),
    "php": (["index.php", "main.php", "app.php", "bot.php"], (".php",)),
    "rust": (["main.rs", "bot.rs"], (".rs",)),
}


def flatten_single_subdir(work_dir):
    """If the zip extracted into a single wrapping folder, move everything up
    one level so entry-point detection works regardless of how it was zipped."""
    try:
        entries = [e for e in os.listdir(work_dir) if e not in IGNORED_DIR_NAMES]
        if len(entries) == 1:
            inner = os.path.join(work_dir, entries[0])
            if os.path.isdir(inner):
                for item in os.listdir(inner):
                    shutil.move(os.path.join(inner, item), os.path.join(work_dir, item))
                shutil.rmtree(inner, ignore_errors=True)
    except Exception:
        logger.exception("flatten_single_subdir failed")


def walk_files(work_dir, suffixes):
    if isinstance(suffixes, str):
        suffixes = (suffixes,)
    matches = []
    for root, dirs, files in os.walk(work_dir):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIR_NAMES]
        for f in files:
            if f.endswith(suffixes):
                matches.append(os.path.join(root, f))
    return matches


def find_entry_file(work_dir, candidates, suffixes):
    for c in candidates:
        p = os.path.join(work_dir, c)
        if os.path.isfile(p):
            return p
    for root, dirs, files in os.walk(work_dir):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIR_NAMES]
        for c in candidates:
            if c in files:
                return os.path.join(root, c)
    matches = walk_files(work_dir, suffixes)
    if len(matches) == 1:
        return matches[0]
    if matches:
        matches.sort(key=lambda p: p.count(os.sep))
        return matches
    return None


def detect_project_type(work_dir):
    if os.path.exists(os.path.join(work_dir, "requirements.txt")) or walk_files(work_dir, ".py"):
        return "python"
    if os.path.exists(os.path.join(work_dir, "package.json")) or walk_files(work_dir, ".js") or walk_files(work_dir, ".ts"):
        return "node"
    if walk_files(work_dir, ".go"):
        return "go"
    if walk_files(work_dir, ".java"):
        return "java"
    if walk_files(work_dir, ".php"):
        return "php"
    if walk_files(work_dir, ".rs"):
        return "rust"
    return None


def extract_py_imports(path):
    modules = set()
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            source = f.read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for n in node.names:
                    modules.add(n.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.level == 0:
                    modules.add(node.module.split(".")[0])
    except Exception:
        logger.debug("could not parse %s for imports", path, exc_info=True)
    return modules


def find_missing_python_packages(work_dir):
    """Scan every .py file for imports and figure out, purely from source
    analysis, which third-party packages need to be pip-installed —
    independent of whether requirements.txt even exists or is complete."""
    local_names = set()
    for root, dirs, files in os.walk(work_dir):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIR_NAMES]
        for f in files:
            if f.endswith(".py"):
                local_names.add(os.path.splitext(f)[0])
        for d in dirs:
            local_names.add(d)

    all_modules = set()
    for path in walk_files(work_dir, ".py"):
        all_modules |= extract_py_imports(path)

    to_install = []
    for mod in all_modules:
        if not mod or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", mod):
            continue
        if mod in STDLIB_MODULES or mod in local_names:
            continue
        try:
            if importlib.util.find_spec(mod) is not None:
                continue
        except Exception:
            pass
        to_install.append(IMPORT_TO_PIP.get(mod, mod))

    return sorted(set(to_install))


def find_missing_node_packages(work_dir):
    declared = set()
    pkg_json = os.path.join(work_dir, "package.json")
    if os.path.exists(pkg_json):
        try:
            with open(pkg_json, "r", encoding="utf-8", errors="ignore") as f:
                data = json.load(f)
            declared |= set(data.get("dependencies", {}).keys())
            declared |= set(data.get("devDependencies", {}).keys())
        except Exception:
            logger.debug("could not parse package.json", exc_info=True)

    required = set()
    require_re = re.compile(r"require\(\s*['\"]([^./][^'\"]*)['\"]\s*\)")
    import_re = re.compile(r"from\s+['\"]([^./][^'\"]*)['\"]")
    for path in walk_files(work_dir, ".js") + walk_files(work_dir, ".ts"):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            for match in require_re.findall(content) + import_re.findall(content):
                pkg = match.split("/")[0] if not match.startswith("@") else "/".join(match.split("/")[:2])
                required.add(pkg)
        except Exception:
            continue

    return sorted(required - declared)


async def run_and_log(cmd, cwd):
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        return proc.returncode, (out or b"").decode(errors="ignore")
    except Exception as e:
        return -1, str(e)


# ----------------------------------------------------
# SECURITY / WEBHOOK / PROGRESS HELPERS (added)
# ----------------------------------------------------
def secure_extract_zip(zip_path, extract_dir, max_total=None):
    """Zip-slip-safe extraction: no member may escape extract_dir, and total
    uncompressed size is capped so a zip-bomb cannot flood the disk."""
    base = os.path.realpath(extract_dir)
    total = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            total += info.file_size
            if max_total is not None and total > max_total:
                raise ValueError("archive too large (possible zip bomb)")
        for info in zf.infolist():
            member = info.filename.replace("\\", "/")
            target = os.path.realpath(os.path.join(extract_dir, member))
            if target != base and not target.startswith(base + os.sep):
                raise ValueError(f"unsafe zip entry: {info.filename!r}")
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)


async def kill_process_tree(process):
    """Terminate the process and its whole group so uploaded code cannot
    leave orphan children running after it is stopped."""
    pid = process.pid
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        try:
            process.terminate()
        except Exception:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except Exception:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


WEBHOOK_CONFIG_NAMES = ("webhook.txt", "webhook.url", ".webhook")


def find_configured_webhook(work_dir, project_type):
    """Automatically discover a webhook URL the user put inside the project
    (webhook.txt / webhook.url / .webhook / .env WEBHOOK_URL=...)."""
    for name in WEBHOOK_CONFIG_NAMES:
        p = os.path.join(work_dir, name)
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    candidate = (f.read().strip().splitlines() or [""])[0].strip()
                if candidate.startswith(("http://", "https://")):
                    return candidate
            except Exception:
                continue
    env_file = os.path.join(work_dir, ".env")
    if os.path.isfile(env_file):
        try:
            with open(env_file, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("WEBHOOK_URL="):
                        url = line.split("=", 1)[1].strip().strip("\"")
                        if url.startswith(("http://", "https://")):
                            return url
        except Exception:
            pass
    return None


def _ping_webhook(url, pid, file_name):
    import urllib.request
    req = urllib.request.Request(
        url,
        data=json.dumps({"event": "started", "pid": pid, "file": file_name}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read(1024)


# Fully automatic Telegram bot-token discovery inside uploaded projects
TELEGRAM_TOKEN_RE = re.compile(r"\b[0-9]{6,12}:[A-Za-z0-9_\-]{30,70}\b")


def _scan_files_for_pattern(work_dir, suffixes, pattern, limit=40):
    found = []
    for root, dirs, files in os.walk(work_dir):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIR_NAMES]
        for f in files:
            if not f.endswith(suffixes):
                continue
            p = os.path.join(root, f)
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                    content = fh.read(512 * 1024)
            except Exception:
                continue
            for m in re.finditer(pattern, content):
                found.append((p, m.group(0)))
                if len(found) >= limit:
                    return found
    return found


def find_php_bot_token(work_dir):
    """Fully automatic: find a Telegram bot token inside any PHP/config/.env
    file of the uploaded project so the runner can call setWebhook for it."""
    suffixes = (".php", ".json", ".env", ".ini", ".cfg", ".conf", ".txt")
    hits = _scan_files_for_pattern(work_dir, suffixes, TELEGRAM_TOKEN_RE)
    if not hits:
        return None
    # never touch the runner's own bot token
    for path, tok in hits:
        if tok != BOT_TOKEN:
            return tok
    return hits[0][1]


def find_project_host_url(work_dir):
    """Fully automatic: locate the public URL where the uploaded PHP bot is (or
    will be) hosted, from .env / config.php / settings.php."""
    env_file = os.path.join(work_dir, ".env")
    candidates = {}
    if os.path.isfile(env_file):
        try:
            for line in open(env_file, encoding="utf-8", errors="ignore"):
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                candidates[k.strip().upper()] = v.strip().strip('"').strip("'")
        except Exception:
            pass
    for key in ("WEBHOOK_URL", "APP_URL", "PUBLIC_URL", "SITE_URL", "BASE_URL", "BOT_URL", "DOMAIN"):
        val = candidates.get(key)
        if val and val.startswith(("http://", "https://")):
            return val
    for name in ("config.php", "settings.php", "config.inc.php", "bot.php", "index.php"):
        p = os.path.join(work_dir, name)
        if not os.path.isfile(p):
            continue
        try:
            content = open(p, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        m = re.search(r"https?://\S+", content, re.I)
        if m:
            return m.group(0).rstrip(";,)").rstrip(chr(39) + chr(34))
    return None


def _call_telegram_set_webhook(token, url):
    import urllib.request
    import urllib.parse
    api = "https://api.telegram.org/bot{}/setWebhook".format(token)
    data = urllib.parse.urlencode({"url": url}).encode()
    req = urllib.request.Request(api, data=data)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read(512).decode(errors="ignore")


async def auto_setup_webhook(work_dir, project_type, pid, file_name, user_id):
    # 1) declared webhook (webhook.txt / .webhook / WEBHOOK_URL) - as before
    url = find_configured_webhook(work_dir, project_type)
    source = "declared"

    # 2) FULLY AUTOMATIC: PHP projects that carry their own Telegram bot token
    if not url and project_type == "php":
        token = find_php_bot_token(work_dir)
        host = find_project_host_url(work_dir)
        if token and host:
            try:
                reply = await asyncio.to_thread(_call_telegram_set_webhook, token, host)
                if '"ok":true' in reply:
                    url = host
                    source = "telegram-setwebhook"
                    logger.info("AUTO setWebhook OK pid=%s url=%s", pid, host)
            except Exception:
                logger.warning("automatic setWebhook failed for pid=%s", pid)

    if not url:
        return None
    WEBHOOKS_DB[str(pid)] = {
        "url": url,
        "source": source,
        "file_name": file_name,
        "user_id": user_id,
        "project_type": project_type,
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "events": ["started"],
    }
    save_webhooks_db()
    try:
        await asyncio.to_thread(_ping_webhook, url, pid, file_name)
    except Exception:
        logger.warning("webhook ping failed for pid=%s url=%s", pid, url)
    return url


async def countdown_before_run(status_message, lang):
    """Nice emoji progress messages while the intentional pre-run delay passes."""
    steps = [
        (0,  "🎉 پروژه‌ی جدید دریافت شد!", "🎉 New project received!"),
        (8,  "📂 در حال بررسی فایل‌ها...", "📂 Scanning project files..."),
        (16, "⚙️ در حال آماده‌سازی محیط اجرا...", "⚙️ Preparing runtime environment..."),
        (24, "⭐ تقریباً آماده شد!", "⭐ Almost ready!"),
    ]
    last = 0
    for at, fa_txt, en_txt in steps:
        if at - last > 0:
            await asyncio.sleep(at - last)
        try:
            await status_message.answer(fa_txt if lang == "fa" else en_txt)
        except Exception:
            pass
        last = at
    if RUN_DELAY_SECONDS - last > 0:
        await asyncio.sleep(RUN_DELAY_SECONDS - last)


# Railway's default builder only provisions Python (since that's the only
# language it detects from this project at build time). Any other language
# runtime has to be installed inside the running container on demand.
SYSTEM_RUNTIME_REQUIREMENTS = {
    "node": ("node", ["nodejs", "npm"]),
    "go": ("go", ["golang-go"]),
    "java": ("java", ["default-jdk"]),
    "php": ("php", ["php-cli"]),
    "rust": ("rustc", ["rustc"]),
}


async def ensure_system_runtime(project_type, status_message: types.Message, lang):
    """Make sure the interpreter/compiler needed for project_type exists,
    installing it automatically via apt if it's missing. Never raises —
    on failure it reports clearly and lets the caller decide what to do,
    instead of crashing the bot."""
    if project_type not in SYSTEM_RUNTIME_REQUIREMENTS:
        return True  # python needs nothing extra

    check_cmd, apt_packages = SYSTEM_RUNTIME_REQUIREMENTS[project_type]
    if shutil.which(check_cmd):
        return True

    await status_message.answer(t("runtime_installing", lang).format(type=project_type))

    for prefix in ([], ["sudo"]):
        code, out = await run_and_log([*prefix, "apt-get", "update"], "/")
        if code == 0:
            code, out = await run_and_log([*prefix, "apt-get", "install", "-y", *apt_packages], "/")
            if code == 0 and shutil.which(check_cmd):
                await status_message.answer(t("runtime_installed_success", lang).format(type=project_type))
                return True

    await status_message.answer(t("runtime_install_failed", lang).format(type=project_type))
    return False


async def install_dependencies(work_dir, project_type, status_message: types.Message, lang):
    """Best-effort dependency installation. Never raises — failures are
    logged and reported, but execution still proceeds afterwards."""
    notes = []

    if project_type == "python":
        req_txt = os.path.join(work_dir, "requirements.txt")
        if os.path.exists(req_txt):
            code, out = await run_and_log(
                [sys.executable, "-m", "pip", "install", "--no-input",
                 "--disable-pip-version-check", "-r", "requirements.txt"],
                work_dir,
            )
            if code != 0:
                notes.append(t("req_install_failed", lang).format(out=out[-300:]))

        missing = find_missing_python_packages(work_dir)
        if missing:
            await status_message.answer(t("detected_py_packages", lang).format(pkgs=", ".join(missing)))
            code, out = await run_and_log(
                [sys.executable, "-m", "pip", "install", "--no-input",
                 "--disable-pip-version-check", *missing],
                work_dir,
            )
            if code != 0:
                notes.append(t("detected_py_install_failed", lang).format(out=out[-300:]))

    elif project_type == "node":
        pkg_json = os.path.join(work_dir, "package.json")
        if os.path.exists(pkg_json):
            code, out = await run_and_log(["npm", "install"], work_dir)
            if code != 0:
                notes.append(t("npm_install_failed", lang).format(out=out[-300:]))

        missing = find_missing_node_packages(work_dir)
        if missing:
            await status_message.answer(t("detected_node_packages", lang).format(pkgs=", ".join(missing)))
            code, out = await run_and_log(["npm", "install", *missing], work_dir)
            if code != 0:
                notes.append(t("detected_node_install_failed", lang).format(out=out[-300:]))

    for note in notes:
        try:
            await status_message.answer(note)
        except Exception:
            pass


def build_run_command(entry_path, project_type):
    run_dir = os.path.dirname(entry_path)
    name = os.path.basename(entry_path)

    if project_type == "python":
        cmd = [sys.executable, name]

    elif project_type == "node":
        cmd = (["npx", "ts-node", name] if name.endswith(".ts") else ["node", name])

    elif project_type == "go":
        # List every .go file in the directory as an ad-hoc package so this
        # works for both single-file scripts and multi-file projects, with
        # or without a go.mod.
        go_files = sorted(f for f in os.listdir(run_dir) if f.endswith(".go"))
        cmd = ["go", "run", *go_files] if go_files else ["go", "run", name]

    elif project_type == "php":
        cmd = ["php", name]

    elif project_type == "java":
        # Compile every .java file in the directory together, then run the
        # class matching the chosen entry file (handles multi-file projects
        # without a package declaration).
        class_name = re.sub(r"\W", "_", os.path.splitext(name)[0])
        cmd = ["sh", "-c", "javac *.java && java {}".format(class_name)]

    elif project_type == "rust":
        # rustc follows `mod` declarations from the entry file automatically,
        # so this also covers multi-file rust projects.
        cmd = ["sh", "-c", "rustc {} -o app && ./app".format(shlex.quote(name))]

    else:
        cmd = None

    return cmd, run_dir


# ----------------------------------------------------
# UPLOAD & EXECUTION
# ----------------------------------------------------
@dp.message(F.text.in_([t("upload_run", "fa"), t("upload_run", "en")]))
@safe_handler
async def start_upload(message: types.Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    await state.set_state(UploadState.waiting_for_file)
    await message.answer(t("send_file_prompt", lang), reply_markup=get_cancel_keyboard(lang))


@dp.message(UploadState.waiting_for_file, F.document)
@safe_handler
async def handle_file(message: types.Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    doc = message.document
    file_name = os.path.basename((doc.file_name or "upload").replace("\\", "/")) or "upload"
    logger.info("UPLOAD | user=%s | file=%s", message.from_user.id, file_name)
    ext = os.path.splitext(file_name)[1].lower()
    allowed_exts = [".py", ".js", ".ts", ".go", ".java", ".php", ".rs", ".zip"]

    if ext not in allowed_exts:
        await message.answer(t("unsupported_ext", lang))
        return

    user_projects = sum(1 for p in RUNNING_PROCESSES.values() if p.get("user_id") == message.from_user.id)
    if user_projects >= MAX_USER_PROJECTS:
        await message.answer(t("max_projects_reached", lang).format(max=MAX_USER_PROJECTS))
        await state.clear()
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    work_dir = os.path.join(os.getcwd(), f"run_{message.from_user.id}_{timestamp}")
    os.makedirs(work_dir, exist_ok=True)

    file_path = os.path.join(work_dir, file_name)
    try:
        await bot.download(doc, destination=file_path)
    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        await message.answer(t("download_failed", lang).format(err=str(e)[:200]), reply_markup=get_main_keyboard(lang, is_admin(message.from_user.id)))
        await state.clear()
        return

    entry_path = None
    project_type = None

    if ext == ".zip":
        try:
            secure_extract_zip(file_path, work_dir, MAX_ZIP_UNCOMPRESSED_BYTES)
            os.remove(file_path)
        except Exception as e:
            shutil.rmtree(work_dir, ignore_errors=True)
            await message.answer(t("zip_extract_failed", lang).format(err=str(e)))
            await state.clear()
            return

        flatten_single_subdir(work_dir)
        project_type = detect_project_type(work_dir)

        if project_type is None:
            shutil.rmtree(work_dir, ignore_errors=True)
            await message.answer(t("project_type_undetected", lang), reply_markup=get_main_keyboard(lang, is_admin(message.from_user.id)))
            await state.clear()
            return

        candidates, suffixes = ENTRY_CONFIG[project_type]
        found = find_entry_file(work_dir, candidates, suffixes)

        if isinstance(found, list):
            # Multiple files, no clear single entry point — let the user choose.
            rel_choices = [os.path.relpath(p, work_dir) for p in found[:20]]
            await state.update_data(
                work_dir=work_dir, project_type=project_type, entry_choices=rel_choices
            )
            await state.set_state(UploadState.waiting_for_entry_choice)
            buttons = [[kbtn(c, "primary")] for c in rel_choices]
            buttons.append([kbtn(t("cancel_btn", lang), "danger")])
            await message.answer(
                t("multiple_entry_files", lang),
                reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True),
            )
            return
        entry_path = found

        if not entry_path:
            shutil.rmtree(work_dir, ignore_errors=True)
            await message.answer(t("no_runnable_file", lang), reply_markup=get_main_keyboard(lang, is_admin(message.from_user.id)))
            await state.clear()
            return
    else:
        entry_path = file_path
        project_type = {
            ".py": "python", ".js": "node", ".ts": "node",
            ".go": "go", ".php": "php", ".java": "java", ".rs": "rust",
        }[ext]

    await state.update_data(
        work_dir=work_dir, project_type=project_type,
        entry_path=entry_path, file_name=os.path.basename(entry_path),
    )
    await state.set_state(UploadState.waiting_for_duration_unit)
    await message.answer(t("select_runtime_unit", lang), reply_markup=get_time_unit_keyboard(lang))


@dp.message(UploadState.waiting_for_entry_choice)
@safe_handler
async def entry_choice_selected(message: types.Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    data = await state.get_data()
    choices = data.get("entry_choices", [])
    if message.text not in choices:
        await message.answer(t("choose_shown_option", lang))
        return

    work_dir = data["work_dir"]
    entry_path = os.path.join(work_dir, message.text)
    await state.update_data(entry_path=entry_path, file_name=os.path.basename(entry_path))
    await state.set_state(UploadState.waiting_for_duration_unit)
    await message.answer(t("select_runtime_unit", lang), reply_markup=get_time_unit_keyboard(lang))


@dp.message(UploadState.waiting_for_duration_unit, F.text.in_([
    t("hours_btn", "fa"), t("hours_btn", "en"), t("days_btn", "fa"), t("days_btn", "en"),
]))
@safe_handler
async def select_duration_unit(message: types.Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    unit = "hours" if message.text in (t("hours_btn", "fa"), t("hours_btn", "en")) else "days"
    await state.update_data(time_unit=unit)
    await state.set_state(UploadState.waiting_for_duration_value)
    unit_label = t("hours_btn", lang) if unit == "hours" else t("days_btn", lang)
    await message.answer(t("enter_duration", lang).format(unit=unit_label), reply_markup=get_cancel_keyboard(lang))


@dp.message(UploadState.waiting_for_duration_value)
@safe_handler
async def run_project_final(message: types.Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    if not message.text.isdigit() or int(message.text) <= 0:
        await message.answer(t("invalid_positive_integer", lang))
        return

    duration_val = int(message.text)
    data = await state.get_data()
    work_dir = data["work_dir"]
    project_type = data["project_type"]
    entry_path = data["entry_path"]
    file_name = data["file_name"]
    unit = data["time_unit"]

    duration_seconds = duration_val * 3600 if unit == "hours" else duration_val * 86400
    end_time = datetime.now() + timedelta(seconds=duration_seconds)

    status_message = await message.answer(t("installing_packages", lang))

    # The user asked for a deliberate delay before anything really runs,
    # with nice emoji progress messages.
    await countdown_before_run(status_message, lang)

    runtime_ok = await ensure_system_runtime(project_type, status_message, lang)
    if not runtime_ok:
        shutil.rmtree(work_dir, ignore_errors=True)
        await message.answer(t("runtime_env_unsupported", lang), reply_markup=get_main_keyboard(lang, is_admin(message.from_user.id)))
        await state.clear()
        return

    await install_dependencies(work_dir, project_type, status_message, lang)

    cmd, run_dir = build_run_command(entry_path, project_type)
    if not cmd:
        shutil.rmtree(work_dir, ignore_errors=True)
        await message.answer(t("project_type_unsupported", lang), reply_markup=get_main_keyboard(lang, is_admin(message.from_user.id)))
        await state.clear()
        return

    try:
        process = await asyncio.create_subprocess_exec(*cmd, cwd=run_dir, start_new_session=True)
        pid = process.pid

        task = asyncio.create_task(auto_stop_project(pid, duration_seconds, file_name, work_dir))

        RUNNING_PROCESSES[pid] = {
            "process": process,
            "file_name": file_name,
            "work_dir": work_dir,
            "user_id": message.from_user.id,
            "end_time": end_time,
            "task": task,
        }
        save_db()

        webhook_url = await auto_setup_webhook(
            work_dir, project_type, pid, file_name, message.from_user.id
        )
        if webhook_url:
            try:
                await message.answer(t("webhook_ready", lang))
            except Exception:
                pass

        logger.info("RUNNING | user=%s | pid=%s | file=%s", message.from_user.id, pid, file_name)

        webhook_line = f"\n🔗 **Webhook:** `{webhook_url}`" if webhook_url else ""
        await message.answer(
            t("project_running", lang).format(name=file_name, pid=pid, end=end_time.strftime("%Y-%m-%d %H:%M:%S")) + webhook_line,
            reply_markup=get_main_keyboard(lang, is_admin(message.from_user.id)),
        )
    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        await message.answer(t("execution_error", lang).format(err=str(e)[:300]), reply_markup=get_main_keyboard(lang, is_admin(message.from_user.id)))

    await state.clear()


async def auto_stop_project(pid, delay, file_name, work_dir):
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    if pid in RUNNING_PROCESSES:
        p_info = RUNNING_PROCESSES[pid]
        await kill_process_tree(p_info["process"])

        shutil.rmtree(work_dir, ignore_errors=True)
        del RUNNING_PROCESSES[pid]
        save_db()
        logger.info("STOPPED | user=%s | pid=%s", p_info["user_id"], pid)

        try:
            await bot.send_message(
                ADMIN_ID, f"⏰ **Time Expired!** Project `{file_name}` (PID: `{pid}`) was automatically stopped."
            )
        except Exception:
            pass


# ----------------------------------------------------
# PROJECT MANAGEMENT
# ----------------------------------------------------
@dp.message(F.text.in_([t("my_projects", "fa"), t("my_projects", "en")]))
@safe_handler
async def my_projects(message: types.Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    if not RUNNING_PROCESSES:
        await message.answer(t("no_active_projects", lang))
        return

    text = t("active_projects_header", lang)
    buttons = []
    for pid, info in RUNNING_PROCESSES.items():
        if info.get("user_id") != message.from_user.id and message.from_user.id != ADMIN_ID:
            continue
        rem = info["end_time"] - datetime.now()
        mins = max(0, int(rem.total_seconds() // 60))
        text += f"🔹🆔 **PID:** `{pid}` | `{info['file_name']}` | {t('remaining_label', lang)}: `{mins}m`\n"
        buttons.append([kbtn(f"{t('manage_pid_prefix', lang)}{pid}", "primary")])

    buttons.append([kbtn(t("cancel_btn", lang), "danger")])
    await state.set_state(ProjectManageState.waiting_for_project_selection)
    await message.answer(text, reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True))


@dp.message(ProjectManageState.waiting_for_project_selection)
@safe_handler
async def select_project(message: types.Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    match = re.search(r"(\d+)\s*$", message.text or "")
    if not match:
        await message.answer(t("choose_shown_option", lang))
        return
    pid = int(match.group(1))

    if pid not in RUNNING_PROCESSES:
        await message.answer(t("no_active_projects", lang))
        await state.clear()
        return

    if RUNNING_PROCESSES[pid].get("user_id") != message.from_user.id and message.from_user.id != ADMIN_ID:
        await message.answer(t("not_admin_access", lang))
        await state.clear()
        return

    await state.update_data(selected_pid=pid)
    await state.set_state(ProjectManageState.waiting_for_action)
    await message.answer(t("action_for_pid", lang).format(pid=pid), reply_markup=get_manage_action_keyboard(lang))


@dp.message(ProjectManageState.waiting_for_action, F.text.in_([t("stop_delete_btn", "fa"), t("stop_delete_btn", "en")]))
@safe_handler
async def stop_project_action(message: types.Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    data = await state.get_data()
    pid = data.get("selected_pid")

    if pid in RUNNING_PROCESSES:
        p_info = RUNNING_PROCESSES[pid]
        p_info["task"].cancel()
        await kill_process_tree(p_info["process"])
        shutil.rmtree(p_info["work_dir"], ignore_errors=True)
        del RUNNING_PROCESSES[pid]
        save_db()
        await message.answer(t("pid_stopped", lang).format(pid=pid), reply_markup=get_main_keyboard(lang, is_admin(message.from_user.id)))

    await state.clear()


@dp.message(ProjectManageState.waiting_for_action, F.text.in_([t("extend_time_btn", "fa"), t("extend_time_btn", "en")]))
@safe_handler
async def extend_time_prompt(message: types.Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    await state.set_state(ProjectManageState.waiting_for_extend_time)
    await message.answer(t("enter_extra_hours", lang), reply_markup=get_cancel_keyboard(lang))


@dp.message(ProjectManageState.waiting_for_extend_time)
@safe_handler
async def extend_time_action(message: types.Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    if not message.text.isdigit() or int(message.text) <= 0:
        await message.answer(t("invalid_number", lang))
        return

    extra_hours = int(message.text)
    data = await state.get_data()
    pid = data.get("selected_pid")

    if pid in RUNNING_PROCESSES:
        p_info = RUNNING_PROCESSES[pid]
        p_info["end_time"] += timedelta(hours=extra_hours)
        p_info["task"].cancel()
        rem_seconds = (p_info["end_time"] - datetime.now()).total_seconds()
        p_info["task"] = asyncio.create_task(
            auto_stop_project(pid, rem_seconds, p_info["file_name"], p_info["work_dir"])
        )
        save_db()
        await message.answer(
            t("extended_success", lang).format(end=p_info["end_time"].strftime("%Y-%m-%d %H:%M:%S")),
            reply_markup=get_main_keyboard(lang, is_admin(message.from_user.id)),
        )

    await state.clear()


# ----------------------------------------------------
# ADMIN PANEL
# ----------------------------------------------------
@dp.message(F.text.in_([t("admin_panel_btn", "fa"), t("admin_panel_btn", "en")]))
@safe_handler
async def open_admin_panel(message: types.Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    if not is_admin(message.from_user.id):
        await message.answer(t("not_admin_access", lang))
        return
    await state.clear()
    await message.answer(t("admin_panel_title", lang), reply_markup=get_admin_keyboard(lang))


@dp.message(F.text.in_([t("back_btn", "fa"), t("back_btn", "en")]))
@safe_handler
async def admin_back(message: types.Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    await state.clear()
    await message.answer(t("main_menu", lang), reply_markup=get_main_keyboard(lang, is_admin(message.from_user.id)))


@dp.message(F.text.in_([t("search_user_btn", "fa"), t("search_user_btn", "en")]))
@safe_handler
async def admin_search_prompt(message: types.Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminState.waiting_for_search_id)
    await message.answer(t("ask_user_id_search", lang), reply_markup=get_cancel_keyboard(lang))


@dp.message(AdminState.waiting_for_search_id)
@safe_handler
async def admin_search_execute(message: types.Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    target = (message.text or "").strip()
    if not target.isdigit():
        await message.answer(t("invalid_id", lang))
        return

    info = USERS_DB.get(target)
    if not info:
        await message.answer(t("user_not_found", lang), reply_markup=get_admin_keyboard(lang))
        await state.clear()
        return

    active_count = sum(1 for p in RUNNING_PROCESSES.values() if str(p.get("user_id")) == target)
    username = f"@{info['username']}" if info.get("username") else "-"
    status = ("🚫 " + ("بن‌شده" if lang == "fa" else "Banned")) if info.get("banned") else ("✅ " + ("فعال" if lang == "fa" else "Active"))

    if lang == "fa":
        text = (
            f"👤 **اطلاعات کاربر**\n\n"
            f"🆔 **آیدی:** `{target}`\n"
            f"📛 **نام:** {info.get('first_name') or '-'}\n"
            f"🔗 **یوزرنیم:** {username}\n"
            f"🌐 **زبان:** {info.get('lang', 'en')}\n"
            f"📅 **تاریخ عضویت:** {info.get('joined', '-')}\n"
            f"⚙️ **پروژه‌های فعال:** {active_count}\n"
            f"📌 **وضعیت:** {status}"
        )
    else:
        text = (
            f"👤 **User Info**\n\n"
            f"🆔 **ID:** `{target}`\n"
            f"📛 **Name:** {info.get('first_name') or '-'}\n"
            f"🔗 **Username:** {username}\n"
            f"🌐 **Language:** {info.get('lang', 'en')}\n"
            f"📅 **Joined:** {info.get('joined', '-')}\n"
            f"⚙️ **Active Projects:** {active_count}\n"
            f"📌 **Status:** {status}"
        )

    await message.answer(text, reply_markup=get_admin_keyboard(lang))
    await state.clear()


@dp.message(F.text.in_([t("ban_user_btn", "fa"), t("ban_user_btn", "en")]))
@safe_handler
async def admin_ban_prompt(message: types.Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminState.waiting_for_ban_id)
    await message.answer(t("ask_ban_id", lang), reply_markup=get_cancel_keyboard(lang))


@dp.message(AdminState.waiting_for_ban_id)
@safe_handler
async def admin_ban_execute(message: types.Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    target = (message.text or "").strip()
    if not target.isdigit():
        await message.answer(t("invalid_id", lang))
        return

    info = USERS_DB.get(target)
    if not info:
        await message.answer(t("user_not_found", lang), reply_markup=get_admin_keyboard(lang))
        await state.clear()
        return

    if info.get("banned"):
        await message.answer(t("already_banned", lang), reply_markup=get_admin_keyboard(lang))
        await state.clear()
        return

    info["banned"] = True
    save_users_db()
    await message.answer(t("user_banned", lang).format(id=target), reply_markup=get_admin_keyboard(lang))
    await state.clear()


@dp.message(F.text.in_([t("unban_user_btn", "fa"), t("unban_user_btn", "en")]))
@safe_handler
async def admin_unban_prompt(message: types.Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminState.waiting_for_unban_id)
    await message.answer(t("ask_unban_id", lang), reply_markup=get_cancel_keyboard(lang))


@dp.message(AdminState.waiting_for_unban_id)
@safe_handler
async def admin_unban_execute(message: types.Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    target = (message.text or "").strip()
    if not target.isdigit():
        await message.answer(t("invalid_id", lang))
        return

    info = USERS_DB.get(target)
    if not info:
        await message.answer(t("user_not_found", lang), reply_markup=get_admin_keyboard(lang))
        await state.clear()
        return

    if not info.get("banned"):
        await message.answer(t("not_banned", lang), reply_markup=get_admin_keyboard(lang))
        await state.clear()
        return

    info["banned"] = False
    save_users_db()
    await message.answer(t("user_unbanned", lang).format(id=target), reply_markup=get_admin_keyboard(lang))
    await state.clear()


@dp.message(F.text.in_([t("broadcast_btn", "fa"), t("broadcast_btn", "en")]))
@safe_handler
async def admin_broadcast_prompt(message: types.Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminState.waiting_for_broadcast_text)
    await message.answer(t("ask_broadcast_text", lang), reply_markup=get_cancel_keyboard(lang))


@dp.message(AdminState.waiting_for_broadcast_text)
@safe_handler
async def admin_broadcast_execute(message: types.Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    broadcast_text = message.text or ""
    sent, failed = 0, 0
    for uid_str in list(USERS_DB.keys()):
        try:
            await bot.send_message(int(uid_str), broadcast_text)
            sent += 1
        except Exception:
            failed += 1
    await message.answer(t("broadcast_done", lang).format(count=sent, failed=failed), reply_markup=get_admin_keyboard(lang))
    await state.clear()


@dp.message(F.text.in_([t("server_info_admin_btn", "fa"), t("server_info_admin_btn", "en")]))
@safe_handler
async def admin_server_info(message: types.Message):
    lang = get_lang(message.from_user.id)
    if not is_admin(message.from_user.id):
        return
    await message.answer(build_server_status_text(lang), reply_markup=get_admin_keyboard(lang))


@dp.message(F.text.in_([t("all_sources_info_btn", "fa"), t("all_sources_info_btn", "en")]))
@safe_handler
async def admin_all_sources_info(message: types.Message):
    lang = get_lang(message.from_user.id)
    if not is_admin(message.from_user.id):
        return
    if not RUNNING_PROCESSES:
        await message.answer(t("no_sources_running", lang), reply_markup=get_admin_keyboard(lang))
        return

    text = t("all_sources_header", lang)
    for pid, info in RUNNING_PROCESSES.items():
        rem = info["end_time"] - datetime.now()
        mins = max(0, int(rem.total_seconds() // 60))
        text += (
            f"🔹🆔 **PID:** `{pid}` | `{info['file_name']}` | "
            f"👤 `{info['user_id']}` | {t('remaining_label', lang)}: `{mins}m`\n"
        )
    await message.answer(text, reply_markup=get_admin_keyboard(lang))


@dp.message(F.text.in_([t("clear_all_sources_btn", "fa"), t("clear_all_sources_btn", "en")]))
@safe_handler
async def admin_clear_all_sources(message: types.Message):
    lang = get_lang(message.from_user.id)
    if not is_admin(message.from_user.id):
        return

    count = 0
    for pid, p_info in list(RUNNING_PROCESSES.items()):
        p_info["task"].cancel()
        await kill_process_tree(p_info["process"])
        shutil.rmtree(p_info["work_dir"], ignore_errors=True)
        del RUNNING_PROCESSES[pid]
        count += 1
    save_db()
    await message.answer(t("clear_all_done", lang).format(count=count), reply_markup=get_admin_keyboard(lang))


@dp.message(F.text.in_([t("add_admin_btn", "fa"), t("add_admin_btn", "en")]))
@safe_handler
async def admin_add_admin_prompt(message: types.Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminState.waiting_for_add_admin_id)
    await message.answer(t("ask_add_admin_id", lang), reply_markup=get_cancel_keyboard(lang))


@dp.message(AdminState.waiting_for_add_admin_id)
@safe_handler
async def admin_add_admin_execute(message: types.Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    target = (message.text or "").strip()
    if not target.isdigit():
        await message.answer(t("invalid_id", lang))
        return

    target_id = int(target)
    if target_id in ADMINS:
        await message.answer(t("already_admin", lang), reply_markup=get_admin_keyboard(lang))
        await state.clear()
        return

    ADMINS.add(target_id)
    save_admins()
    await message.answer(t("admin_added", lang).format(id=target_id), reply_markup=get_admin_keyboard(lang))
    await state.clear()


# ----------------------------------------------------
# FALLBACK (must stay last — only catches messages no other handler matched)
# ----------------------------------------------------
@dp.message()
@safe_handler
async def fallback_handler(message: types.Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    await message.answer(
        t("fallback_unrecognized", lang),
        reply_markup=get_main_keyboard(lang, is_admin(message.from_user.id)),
    )


# ----------------------------------------------------
# MAIN EXECUTION
# ----------------------------------------------------
async def main():
    cleanup_stale_state()
    load_users_db()
    load_admins()
    load_webhooks_db()
    logger.info("Runner bot starting...")

    try:
        me = await bot.get_me()
        logger.info("Authenticated as @%s (id=%s)", me.username, me.id)
    except Exception:
        logger.critical(
            "Could not authenticate with Telegram using the configured BOT_TOKEN. "
            "Double-check that the token in the source file is correct and was not "
            "regenerated/revoked in BotFather."
        )
        return

    try:
        # A webhook left over from an earlier setup silently blocks getUpdates
        # (polling) with no visible error — always clear it before polling.
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        logger.exception("delete_webhook failed (continuing anyway)")

    while True:
        try:
            await dp.start_polling(bot)
            break
        except Exception:
            logger.exception("Polling crashed — restarting in 5 seconds")
            await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
