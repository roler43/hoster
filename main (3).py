import asyncio
import zipfile
import os
import subprocess
import sys
import signal
import time
import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path
import shutil
import psutil
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, FSInputFile, InputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# Конфигурация
BOT_TOKEN = "7702565826:AAE-s3_TdJazx2mV9BPEFPbMUsr-QZY3WfU"
ADMIN_IDS = [6945488830]  # Список админов (твой ID)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Константы
PROJECTS_DIR = "projects"
USERS_DB = "users.db"
MAX_USER_PROJECTS = 1  # Максимум проектов для обычного пользователя

os.makedirs(PROJECTS_DIR, exist_ok=True)

# ==================== БАЗА ДАННЫХ ====================
def init_database():
    """Инициализация базы данных пользователей"""
    conn = sqlite3.connect(USERS_DB)
    c = conn.cursor()
    
    # Таблица пользователей
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY,
                  username TEXT,
                  first_name TEXT,
                  joined_date TEXT,
                  is_admin INTEGER DEFAULT 0,
                  projects_limit INTEGER DEFAULT 1,
                  total_projects INTEGER DEFAULT 0,
                  last_active TEXT)''')
    
    # Таблица проектов пользователей
    c.execute('''CREATE TABLE IF NOT EXISTS user_projects
                 (project_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  project_name TEXT,
                  created_date TEXT,
                  last_run TEXT,
                  status TEXT DEFAULT 'stopped',
                  FOREIGN KEY (user_id) REFERENCES users (user_id))''')
    
    # Таблица статистики
    c.execute('''CREATE TABLE IF NOT EXISTS stats
                 (stat_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  action TEXT,
                  project_name TEXT,
                  timestamp TEXT,
                  details TEXT)''')
    
    conn.commit()
    conn.close()

def add_user(user_id, username, first_name):
    """Добавление нового пользователя"""
    conn = sqlite3.connect(USERS_DB)
    c = conn.cursor()
    
    # Проверяем, есть ли уже пользователь
    c.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if not c.fetchone():
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        is_admin = 1 if user_id in ADMIN_IDS else 0
        projects_limit = 999 if is_admin else MAX_USER_PROJECTS
        
        c.execute("""INSERT INTO users 
                     (user_id, username, first_name, joined_date, is_admin, projects_limit, last_active)
                     VALUES (?, ?, ?, ?, ?, ?, ?)""",
                  (user_id, username, first_name, now, is_admin, projects_limit, now))
        conn.commit()
    
    conn.close()

def update_user_activity(user_id):
    """Обновление времени последней активности"""
    conn = sqlite3.connect(USERS_DB)
    c = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute("UPDATE users SET last_active = ? WHERE user_id = ?", (now, user_id))
    conn.commit()
    conn.close()

def get_user_stats(user_id):
    """Получение статистики пользователя"""
    conn = sqlite3.connect(USERS_DB)
    c = conn.cursor()
    
    c.execute("""SELECT joined_date, is_admin, projects_limit, total_projects, last_active 
                 FROM users WHERE user_id = ?""", (user_id,))
    user_data = c.fetchone()
    
    c.execute("""SELECT COUNT(*) FROM user_projects WHERE user_id = ?""", (user_id,))
    current_projects = c.fetchone()[0]
    
    c.execute("""SELECT project_name, status, last_run FROM user_projects 
                 WHERE user_id = ? ORDER BY last_run DESC LIMIT 5""", (user_id,))
    recent_projects = c.fetchall()
    
    conn.close()
    
    return {
        "joined": user_data[0] if user_data else "Неизвестно",
        "is_admin": bool(user_data[1]) if user_data else False,
        "projects_limit": user_data[2] if user_data else 1,
        "total_projects": user_data[3] if user_data else 0,
        "last_active": user_data[4] if user_data else "Никогда",
        "current_projects": current_projects,
        "recent_projects": recent_projects
    }

def log_action(user_id, action, project_name="", details=""):
    """Логирование действий"""
    conn = sqlite3.connect(USERS_DB)
    c = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute("""INSERT INTO stats (user_id, action, project_name, timestamp, details)
                 VALUES (?, ?, ?, ?, ?)""",
              (user_id, action, project_name, now, details))
    conn.commit()
    conn.close()

def get_all_users_stats():
    """Получение общей статистики для админа"""
    conn = sqlite3.connect(USERS_DB)
    c = conn.cursor()
    
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1")
    total_admins = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM user_projects")
    total_projects = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM user_projects WHERE status = 'running'")
    running_projects = c.fetchone()[0]
    
    c.execute("""SELECT action, COUNT(*) as cnt FROM stats 
                 WHERE timestamp > datetime('now', '-7 days')
                 GROUP BY action ORDER BY cnt DESC LIMIT 5""")
    top_actions = c.fetchall()
    
    c.execute("""SELECT user_id, COUNT(*) as cnt FROM user_projects 
                 GROUP BY user_id ORDER BY cnt DESC LIMIT 5""")
    top_users = c.fetchall()
    
    conn.close()
    
    return {
        "total_users": total_users,
        "total_admins": total_admins,
        "total_projects": total_projects,
        "running_projects": running_projects,
        "top_actions": top_actions,
        "top_users": top_users
    }

# ==================== СОСТОЯНИЯ FSM ====================
class ProjectStates(StatesGroup):
    waiting_for_project_name = State()
    waiting_for_zip = State()
    waiting_for_file_upload = State()
    waiting_for_new_folder = State()
    waiting_for_rename = State()
    waiting_for_file_edit = State()
    waiting_for_main_file = State()

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def get_user_project_path(user_id, project_name=None):
    """Получает путь к проекту пользователя"""
    if project_name:
        return os.path.join(PROJECTS_DIR, f"user_{user_id}", project_name)
    return os.path.join(PROJECTS_DIR, f"user_{user_id}")

def get_admin_project_path(project_name=None):
    """Получает путь к админскому проекту"""
    if project_name:
        return os.path.join(PROJECTS_DIR, "admin", project_name)
    return os.path.join(PROJECTS_DIR, "admin")

def get_user_projects(user_id):
    """Получает список проектов пользователя"""
    user_path = get_user_project_path(user_id)
    if not os.path.exists(user_path):
        return []
    return [d for d in os.listdir(user_path) 
            if os.path.isdir(os.path.join(user_path, d))]

def get_admin_projects():
    """Получает список админских проектов"""
    admin_path = get_admin_project_path()
    if not os.path.exists(admin_path):
        return []
    return [d for d in os.listdir(admin_path) 
            if os.path.isdir(os.path.join(admin_path, d))]

def can_create_project(user_id):
    """Проверяет, может ли пользователь создать новый проект"""
    if user_id in ADMIN_IDS:
        return True, 999
    
    conn = sqlite3.connect(USERS_DB)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM user_projects WHERE user_id = ?", (user_id,))
    current = c.fetchone()[0]
    conn.close()
    
    return current < MAX_USER_PROJECTS, MAX_USER_PROJECTS - current

def get_project_status(project_path):
    """Проверяет статус проекта"""
    pid_file = os.path.join(project_path, ".pid")
    
    if not os.path.exists(pid_file):
        return "stopped"
    
    try:
        with open(pid_file, 'r') as f:
            pid = int(f.read().strip())
        
        process = psutil.Process(pid)
        if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
            return "running"
        else:
            os.remove(pid_file)
            return "stopped"
    except:
        if os.path.exists(pid_file):
            os.remove(pid_file)
        return "stopped"

def format_size(size_bytes):
    """Форматирует размер файла"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"

def get_folder_contents(folder_path):
    """Получает содержимое папки с сортировкой"""
    items = []
    
    if os.path.exists(folder_path):
        for item in os.listdir(folder_path):
            if item.startswith('.') or item == 'bot.log' or item == '.pid':
                continue
            full_path = os.path.join(folder_path, item)
            is_dir = os.path.isdir(full_path)
            size = os.path.getsize(full_path) if not is_dir else 0
            modified = datetime.fromtimestamp(os.path.getmtime(full_path))
            
            items.append({
                'name': item,
                'path': full_path,
                'is_dir': is_dir,
                'size': size,
                'size_str': format_size(size) if not is_dir else '📁',
                'modified': modified.strftime("%Y-%m-%d %H:%M")
            })
    
    # Сортируем: папки выше, потом файлы
    items.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
    return items

async def run_command(command: list, cwd: str = None):
    """Запускает команду и возвращает вывод"""
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd
        )
        stdout, stderr = await process.communicate()
        return stdout.decode(), stderr.decode(), process.returncode
    except Exception as e:
        return "", str(e), -1

def get_main_file(project_path):
    """Определяет основной файл проекта"""
    config_file = os.path.join(project_path, ".bot_config")
    if os.path.exists(config_file):
        with open(config_file, 'r') as f:
            return f.read().strip()
    
    for file in ["bot.py", "main.py", "app.py", "run.py"]:
        if os.path.exists(os.path.join(project_path, file)):
            return file
    return None

def set_main_file(project_path, filename):
    """Сохраняет основной файл"""
    config_file = os.path.join(project_path, ".bot_config")
    with open(config_file, 'w') as f:
        f.write(filename)

# ==================== КЛАВИАТУРЫ ====================
def get_main_keyboard(is_admin=False):
    """Главная клавиатура"""
    builder = InlineKeyboardBuilder()
    builder.button(text="📁 Мои проекты", callback_data="my_projects")
    
    if is_admin:
        builder.button(text="⚙️ Админ панель", callback_data="admin_panel")
    
    builder.button(text="📊 Моя статистика", callback_data="my_stats")
    builder.button(text="❓ Помощь", callback_data="help")
    builder.adjust(1)
    return builder.as_markup()

def get_projects_keyboard(user_id, is_admin=False):
    """Клавиатура со списком проектов"""
    builder = InlineKeyboardBuilder()
    
    if is_admin:
        # Для админа показываем и свои проекты, и пользовательские
        admin_projects = get_admin_projects()
        for project in admin_projects:
            status = get_project_status(get_admin_project_path(project))
            emoji = "🟢" if status == "running" else "🔴"
            builder.button(text=f"{emoji} 👑 {project}", callback_data=f"admin_project_{project}")
    
    # Проекты текущего пользователя
    user_projects = get_user_projects(user_id)
    for project in user_projects:
        status = get_project_status(get_user_project_path(user_id, project))
        emoji = "🟢" if status == "running" else "🔴"
        builder.button(text=f"{emoji} {project}", callback_data=f"user_project_{user_id}_{project}")
    
    builder.button(text="➕ Новый проект", callback_data="new_project")
    builder.button(text="🔙 Назад", callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()

def get_project_actions_keyboard(project_type, user_id, project_name, current_path=""):
    """Клавиатура действий с проектом"""
    builder = InlineKeyboardBuilder()
    
    # Определяем путь к проекту
    if project_type == "admin":
        project_path = get_admin_project_path(project_name)
    else:
        project_path = get_user_project_path(user_id, project_name)
    
    status = get_project_status(project_path)
    
    # Кнопки управления проектом
    if status == "running":
        builder.button(text="⏹ Остановить", callback_data=f"stop_{project_type}_{user_id}_{project_name}")
        builder.button(text="🔄 Перезапустить", callback_data=f"restart_{project_type}_{user_id}_{project_name}")
    else:
        builder.button(text="▶️ Запустить", callback_data=f"start_{project_type}_{user_id}_{project_name}")
    
    builder.button(text="📦 Установить зависимости", callback_data=f"install_{project_type}_{user_id}_{project_name}")
    builder.button(text="📁 Открыть проводник", callback_data=f"explorer_{project_type}_{user_id}_{project_name}_")
    builder.button(text="⚙️ Выбрать основной файл", callback_data=f"setmain_{project_type}_{user_id}_{project_name}")
    builder.button(text="📋 Логи", callback_data=f"logs_{project_type}_{user_id}_{project_name}")
    builder.button(text="📥 Скачать проект", callback_data=f"download_{project_type}_{user_id}_{project_name}")
    builder.button(text="🗑 Удалить проект", callback_data=f"delete_{project_type}_{user_id}_{project_name}")
    builder.button(text="🔙 Назад к проектам", callback_data="my_projects")
    
    builder.adjust(2)
    return builder.as_markup()

def get_explorer_keyboard(project_type, user_id, project_name, current_path=""):
    """Проводник файлов"""
    builder = InlineKeyboardBuilder()
    
    # Определяем полный путь
    if project_type == "admin":
        base_path = get_admin_project_path(project_name)
    else:
        base_path = get_user_project_path(user_id, project_name)
    
    full_path = os.path.join(base_path, current_path) if current_path else base_path
    
    # Кнопка "Назад"
    if current_path:
        parent_path = os.path.dirname(current_path)
        builder.button(text="📂 ..", callback_data=f"explorer_{project_type}_{user_id}_{project_name}_{parent_path}")
    
    # Получаем содержимое
    items = get_folder_contents(full_path)
    
    for item in items:
        if item['is_dir']:
            new_path = os.path.join(current_path, item['name']) if current_path else item['name']
            builder.button(
                text=f"📁 {item['name']}",
                callback_data=f"explorer_{project_type}_{user_id}_{project_name}_{new_path}"
            )
        else:
            file_path = os.path.join(current_path, item['name']) if current_path else item['name']
            builder.button(
                text=f"📄 {item['name']} ({item['size_str']})",
                callback_data=f"file_{project_type}_{user_id}_{project_name}_{file_path}"
            )
    
    # Кнопки действий
    builder.button(text="📤 Загрузить файл", callback_data=f"upload_{project_type}_{user_id}_{project_name}_{current_path}")
    builder.button(text="➕ Создать папку", callback_data=f"mkdir_{project_type}_{user_id}_{project_name}_{current_path}")
    builder.button(text="🔙 Назад к проекту", callback_data=f"project_{project_type}_{user_id}_{project_name}")
    
    builder.adjust(1)
    return builder.as_markup()

def get_file_actions_keyboard(project_type, user_id, project_name, file_path):
    """Клавиатура действий с файлом"""
    builder = InlineKeyboardBuilder()
    
    builder.button(text="📥 Скачать", callback_data=f"getfile_{project_type}_{user_id}_{project_name}_{file_path}")
    builder.button(text="✏️ Переименовать", callback_data=f"rename_{project_type}_{user_id}_{project_name}_{file_path}")
    builder.button(text="🗑 Удалить", callback_data=f"delfile_{project_type}_{user_id}_{project_name}_{file_path}")
    builder.button(text="🔙 Назад", callback_data=f"explorer_{project_type}_{user_id}_{project_name}_{os.path.dirname(file_path)}")
    
    # Для Python файлов добавляем кнопку "Сделать основным"
    if file_path.endswith('.py'):
        builder.button(text="⭐ Сделать основным", callback_data=f"make_main_{project_type}_{user_id}_{project_name}_{file_path}")
    
    builder.adjust(2)
    return builder.as_markup()

def get_admin_panel_keyboard():
    """Админ панель"""
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Общая статистика", callback_data="admin_stats")
    builder.button(text="👥 Все пользователи", callback_data="admin_users")
    builder.button(text="📁 Все проекты", callback_data="admin_all_projects")
    builder.button(text="📋 Логи действий", callback_data="admin_logs")
    builder.button(text="⚙️ Настройки", callback_data="admin_settings")
    builder.button(text="🔙 Назад", callback_data="main_menu")
    builder.adjust(2)
    return builder.as_markup()

# ==================== ОБРАБОТЧИКИ ====================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username or "Нет username"
    first_name = message.from_user.first_name
    
    # Добавляем пользователя в БД
    add_user(user_id, username, first_name)
    update_user_activity(user_id)
    
    is_admin = user_id in ADMIN_IDS
    
    welcome_text = (
        f"👋 Привет, {first_name}!\n\n"
        f"Это хостинг для твоих Telegram ботов. Здесь ты можешь:\n"
        f"• Загружать свои проекты\n"
        f"• Управлять файлами как в проводнике\n"
        f"• Запускать/останавливать ботов\n"
        f"• Смотреть логи\n\n"
    )
    
    if is_admin:
        welcome_text += "👑 У тебя админские права! Тебе доступны все функции и неограниченное количество проектов."
    else:
        welcome_text += f"📦 Для обычных пользователей: максимум {MAX_USER_PROJECTS} проект."
    
    await message.answer(welcome_text, reply_markup=get_main_keyboard(is_admin))

@dp.callback_query(lambda c: c.data == "main_menu")
async def main_menu(callback: CallbackQuery):
    user_id = callback.from_user.id
    update_user_activity(user_id)
    is_admin = user_id in ADMIN_IDS
    
    await callback.message.edit_text(
        "🏠 Главное меню:",
        reply_markup=get_main_keyboard(is_admin)
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "help")
async def help_menu(callback: CallbackQuery):
    user_id = callback.from_user.id
    is_admin = user_id in ADMIN_IDS
    
    help_text = (
        "❓ *Помощь по использованию*\n\n"
        "*📁 Мои проекты* - список твоих проектов\n"
        "*➕ Новый проект* - загрузить новый проект (ZIP архив)\n"
        "*📊 Моя статистика* - информация о твоей активности\n\n"
        
        "*В проводнике файлов:*\n"
        "• Навигация по папкам как в обычном проводнике\n"
        "• Загрузка файлов через кнопку 'Загрузить файл'\n"
        "• Создание папок\n"
        "• Переименование и удаление\n\n"
        
        "*Управление проектом:*\n"
        "• Запуск/остановка бота\n"
        "• Установка зависимостей из requirements.txt\n"
        "• Просмотр логов (отправляются файлом)\n"
        "• Выбор основного файла для запуска\n"
    )
    
    if is_admin:
        help_text += "\n👑 *Админ функции:*\n• Просмотр статистики\n• Управление всеми проектами\n• Логи действий пользователей"
    
    await callback.message.edit_text(
        help_text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardBuilder().button(text="🔙 Назад", callback_data="main_menu").as_markup()
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "my_stats")
async def my_stats(callback: CallbackQuery):
    user_id = callback.from_user.id
    stats = get_user_stats(user_id)
    
    text = (
        f"📊 *Твоя статистика*\n\n"
        f"👤 ID: `{user_id}`\n"
        f"📅 Присоединился: `{stats['joined']}`\n"
        f"🕐 Последний визит: `{stats['last_active']}`\n"
        f"👑 Админ: {'✅' if stats['is_admin'] else '❌'}\n\n"
        
        f"📁 Проектов: `{stats['current_projects']}/{stats['projects_limit']}`\n"
        f"📦 Всего создано: `{stats['total_projects']}`\n\n"
    )
    
    if stats['recent_projects']:
        text += "*Последние проекты:*\n"
        for p in stats['recent_projects']:
            status = "🟢" if p[1] == "running" else "🔴"
            last_run = p[2] if p[2] else "Никогда"
            text += f"{status} `{p[0]}` (последний запуск: {last_run})\n"
    
    await callback.message.edit_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardBuilder().button(text="🔙 Назад", callback_data="main_menu").as_markup()
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "my_projects")
async def my_projects(callback: CallbackQuery):
    user_id = callback.from_user.id
    is_admin = user_id in ADMIN_IDS
    
    await callback.message.edit_text(
        "📁 Твои проекты:",
        reply_markup=get_projects_keyboard(user_id, is_admin)
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "new_project")
async def new_project_prompt(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    can_create, remaining = can_create_project(user_id)
    
    if not can_create and user_id not in ADMIN_IDS:
        await callback.answer(f"❌ Ты достиг лимита проектов ({MAX_USER_PROJECTS})!", show_alert=True)
        return
    
    await state.set_state(ProjectStates.waiting_for_project_name)
    await callback.message.edit_text(
        "📝 Введи название для нового проекта:\n"
        "(только буквы, цифры и нижнее подчеркивание)\n\n"
        "Для отмены отправь /cancel"
    )
    await callback.answer()

@dp.message(ProjectStates.waiting_for_project_name)
async def get_project_name(message: Message, state: FSMContext):
    user_id = message.from_user.id
    project_name = message.text.strip()
    
    # Проверяем имя
    if not project_name.replace('_', '').replace('-', '').isalnum():
        await message.answer("❌ Имя может содержать только буквы, цифры, _ и -")
        return
    
    if user_id in ADMIN_IDS:
        project_path = get_admin_project_path(project_name)
    else:
        project_path = get_user_project_path(user_id, project_name)
    
    if os.path.exists(project_path):
        await message.answer("❌ Проект с таким именем уже существует! Придумай другое имя.")
        return
    
    # Создаем папку
    os.makedirs(project_path, exist_ok=True)
    
    # Сохраняем в БД
    conn = sqlite3.connect(USERS_DB)
    c = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute("""INSERT INTO user_projects (user_id, project_name, created_date, status)
                 VALUES (?, ?, ?, ?)""", (user_id, project_name, now, "stopped"))
    c.execute("UPDATE users SET total_projects = total_projects + 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    
    log_action(user_id, "create_project", project_name)
    
    await state.clear()
    await message.answer(
        f"✅ Проект '{project_name}' создан!\n\n"
        f"Теперь ты можешь загрузить в него файлы через проводник."
    )
    
    # Показываем проекты
    is_admin = user_id in ADMIN_IDS
    await message.answer(
        "📁 Твои проекты:",
        reply_markup=get_projects_keyboard(user_id, is_admin)
    )

@dp.callback_query(lambda c: c.data.startswith("user_project_"))
async def user_project_details(callback: CallbackQuery):
    _, _, user_id, project_name = callback.data.split("_", 3)
    user_id = int(user_id)
    current_user = callback.from_user.id
    
    # Проверяем права
    if current_user != user_id and current_user not in ADMIN_IDS:
        await callback.answer("⛔ У тебя нет доступа к этому проекту!", show_alert=True)
        return
    
    project_path = get_user_project_path(user_id, project_name)
    
    if not os.path.exists(project_path):
        await callback.answer("❌ Проект не найден!", show_alert=True)
        return
    
    # Собираем статистику
    total_size = 0
    file_count = 0
    folder_count = 0
    
    for root, dirs, files in os.walk(project_path):
        folder_count += len(dirs)
        file_count += len(files)
        for f in files:
            fp = os.path.join(root, f)
            total_size += os.path.getsize(fp)
    
    status = get_project_status(project_path)
    status_emoji = "🟢" if status == "running" else "🔴"
    main_file = get_main_file(project_path) or "Не выбран"
    
    # Информация из БД
    conn = sqlite3.connect(USERS_DB)
    c = conn.cursor()
    c.execute("""SELECT created_date, last_run FROM user_projects 
                 WHERE user_id = ? AND project_name = ?""", (user_id, project_name))
    db_info = c.fetchone()
    conn.close()
    
    created = db_info[0] if db_info else "Неизвестно"
    last_run = db_info[1] if db_info and db_info[1] else "Никогда"
    
    text = (
        f"📁 *Проект: {project_name}*\n"
        f"{status_emoji} Статус: `{status}`\n"
        f"👤 Владелец: `{user_id}`\n"
        f"📅 Создан: `{created}`\n"
        f"🕐 Последний запуск: `{last_run}`\n"
        f"📦 Размер: `{format_size(total_size)}`\n"
        f"📄 Файлов: `{file_count}`\n"
        f"📁 Папок: `{folder_count}`\n"
        f"⚙️ Основной файл: `{main_file}`\n\n"
        "Выбери действие:"
    )
    
    await callback.message.edit_text(
        text,
        reply_markup=get_project_actions_keyboard("user", user_id, project_name),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("admin_project_"))
async def admin_project_details(callback: CallbackQuery):
    _, _, project_name = callback.data.split("_", 2)
    
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Это админский проект!", show_alert=True)
        return
    
    project_path = get_admin_project_path(project_name)
    
    if not os.path.exists(project_path):
        await callback.answer("❌ Проект не найден!", show_alert=True)
        return
    
    # Собираем статистику
    total_size = 0
    file_count = 0
    folder_count = 0
    
    for root, dirs, files in os.walk(project_path):
        folder_count += len(dirs)
        file_count += len(files)
        for f in files:
            fp = os.path.join(root, f)
            total_size += os.path.getsize(fp)
    
    status = get_project_status(project_path)
    status_emoji = "🟢" if status == "running" else "🔴"
    main_file = get_main_file(project_path) or "Не выбран"
    
    text = (
        f"👑 *Админ проект: {project_name}*\n"
        f"{status_emoji} Статус: `{status}`\n"
        f"📦 Размер: `{format_size(total_size)}`\n"
        f"📄 Файлов: `{file_count}`\n"
        f"📁 Папок: `{folder_count}`\n"
        f"⚙️ Основной файл: `{main_file}`\n\n"
        "Выбери действие:"
    )
    
    await callback.message.edit_text(
        text,
        reply_markup=get_project_actions_keyboard("admin", callback.from_user.id, project_name),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("explorer_"))
async def open_explorer(callback: CallbackQuery):
    _, _, project_type, user_id, project_name, current_path = callback.data.split("_", 5)
    user_id = int(user_id) if user_id != 'None' else None
    
    await callback.message.edit_text(
        f"📁 {project_name} - {current_path if current_path else 'Корень'}",
        reply_markup=get_explorer_keyboard(project_type, user_id, project_name, current_path)
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("file_"))
async def file_details(callback: CallbackQuery):
    _, _, project_type, user_id, project_name, file_path = callback.data.split("_", 5)
    user_id = int(user_id) if user_id != 'None' else None
    
    # Определяем полный путь
    if project_type == "admin":
        base_path = get_admin_project_path(project_name)
    else:
        base_path = get_user_project_path(user_id, project_name)
    
    full_path = os.path.join(base_path, file_path)
    
    if not os.path.exists(full_path):
        await callback.answer("❌ Файл не найден!", show_alert=True)
        return
    
    # Информация о файле
    size = os.path.getsize(full_path)
    modified = datetime.fromtimestamp(os.path.getmtime(full_path))
    
    # Пытаемся прочитать первые строки для текстовых файлов
    preview = ""
    if file_path.endswith(('.py', '.txt', '.json', '.md', '.yml', '.yaml', '.cfg', '.conf')):
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                if lines:
                    preview_lines = lines[:10]
                    preview = "```\n" + ''.join(preview_lines)
                    if len(lines) > 10:
                        preview += "...\n(показаны первые 10 строк)"
                    preview += "\n```"
        except:
            preview = "❌ Не удалось прочитать файл"
    
    text = (
        f"📄 *{os.path.basename(file_path)}*\n\n"
        f"📦 Размер: `{format_size(size)}`\n"
        f"🕐 Изменен: `{modified.strftime('%Y-%m-%d %H:%M:%S')}`\n"
        f"📍 Путь: `{file_path}`\n\n"
    )
    
    if preview:
        text += f"*Предпросмотр:*\n{preview}\n"
    
    await callback.message.edit_text(
        text,
        parse_mode="Markdown",
        reply_markup=get_file_actions_keyboard(project_type, user_id, project_name, file_path)
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("upload_"))
async def upload_file_prompt(callback: CallbackQuery, state: FSMContext):
    _, _, project_type, user_id, project_name, current_path = callback.data.split("_", 5)
    user_id = int(user_id) if user_id != 'None' else None
    
    await state.set_state(ProjectStates.waiting_for_file_upload)
    await state.update_data(
        project_type=project_type,
        target_user_id=user_id,
        project_name=project_name,
        current_path=current_path
    )
    
    await callback.message.edit_text(
        "📤 Отправь файл, который хочешь загрузить.\n"
        "Это может быть любой файл (код, изображение, документ).\n\n"
        "Для отмены отправь /cancel"
    )
    await callback.answer()

@dp.message(ProjectStates.waiting_for_file_upload, F.document)
async def handle_file_upload(message: Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    
    project_type = data['project_type']
    target_user_id = data['target_user_id']
    project_name = data['project_name']
    current_path = data['current_path']
    
    # Определяем путь назначения
    if project_type == "admin":
        base_path = get_admin_project_path(project_name)
    else:
        base_path = get_user_project_path(target_user_id, project_name)
    
    dest_folder = os.path.join(base_path, current_path) if current_path else base_path
    os.makedirs(dest_folder, exist_ok=True)
    
    # Скачиваем файл
    document = message.document
    file_path = os.path.join(dest_folder, document.file_name)
    
    # Проверяем, существует ли уже
    if os.path.exists(file_path):
        # Добавляем суффикс
        name, ext = os.path.splitext(document.file_name)
        counter = 1
        while os.path.exists(os.path.join(dest_folder, f"{name}_{counter}{ext}")):
            counter += 1
        file_path = os.path.join(dest_folder, f"{name}_{counter}{ext}")
    
    file = await bot.get_file(document.file_id)
    await bot.download_file(file.file_path, file_path)
    
    log_action(user_id, "upload_file", project_name, f"{current_path}/{document.file_name}")
    
    await state.clear()
    await message.answer(f"✅ Файл загружен: {os.path.basename(file_path)}")
    
    # Возвращаемся в проводник
    await message.answer(
        f"📁 {project_name} - {current_path if current_path else 'Корень'}",
        reply_markup=get_explorer_keyboard(project_type, target_user_id, project_name, current_path)
    )

@dp.callback_query(lambda c: c.data.startswith("mkdir_"))
async def create_folder_prompt(callback: CallbackQuery, state: FSMContext):
    _, _, project_type, user_id, project_name, current_path = callback.data.split("_", 5)
    user_id = int(user_id) if user_id != 'None' else None
    
    await state.set_state(ProjectStates.waiting_for_new_folder)
    await state.update_data(
        project_type=project_type,
        target_user_id=user_id,
        project_name=project_name,
        current_path=current_path
    )
    
    await callback.message.edit_text(
        "📁 Введи название новой папки:\n\n"
        "Для отмены отправь /cancel"
    )
    await callback.answer()

@dp.message(ProjectStates.waiting_for_new_folder)
async def create_folder(message: Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    
    project_type = data['project_type']
    target_user_id = data['target_user_id']
    project_name = data['project_name']
    current_path = data['current_path']
    folder_name = message.text.strip()
    
    # Проверяем имя
    if not folder_name.replace('_', '').replace('-', '').replace('.', '').isalnum():
        await message.answer("❌ Имя может содержать только буквы, цифры, _, - и .")
        return
    
    # Определяем путь
    if project_type == "admin":
        base_path = get_admin_project_path(project_name)
    else:
        base_path = get_user_project_path(target_user_id, project_name)
    
    folder_path = os.path.join(base_path, current_path, folder_name) if current_path else os.path.join(base_path, folder_name)
    
    if os.path.exists(folder_path):
        await message.answer("❌ Папка с таким именем уже существует!")
        return
    
    os.makedirs(folder_path)
    
    log_action(user_id, "create_folder", project_name, f"{current_path}/{folder_name}")
    
    await state.clear()
    await message.answer(f"✅ Папка '{folder_name}' создана!")
    
    # Возвращаемся в проводник
    await message.answer(
        f"📁 {project_name} - {current_path if current_path else 'Корень'}",
        reply_markup=get_explorer_keyboard(project_type, target_user_id, project_name, current_path)
    )

@dp.callback_query(lambda c: c.data.startswith("rename_"))
async def rename_file_prompt(callback: CallbackQuery, state: FSMContext):
    _, _, project_type, user_id, project_name, file_path = callback.data.split("_", 5)
    user_id = int(user_id) if user_id != 'None' else None
    
    await state.set_state(ProjectStates.waiting_for_rename)
    await state.update_data(
        project_type=project_type,
        target_user_id=user_id,
        project_name=project_name,
        file_path=file_path
    )
    
    await callback.message.edit_text(
        f"✏️ Введи новое имя для:\n`{os.path.basename(file_path)}`\n\n"
        "Для отмены отправь /cancel",
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.message(ProjectStates.waiting_for_rename)
async def rename_file(message: Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    
    project_type = data['project_type']
    target_user_id = data['target_user_id']
    project_name = data['project_name']
    file_path = data['file_path']
    new_name = message.text.strip()
    
    # Проверяем имя
    if not new_name.replace('_', '').replace('-', '').replace('.', '').isalnum():
        await message.answer("❌ Имя может содержать только буквы, цифры, _, - и .")
        return
    
    # Определяем пути
    if project_type == "admin":
        base_path = get_admin_project_path(project_name)
    else:
        base_path = get_user_project_path(target_user_id, project_name)
    
    old_full_path = os.path.join(base_path, file_path)
    folder = os.path.dirname(old_full_path)
    new_full_path = os.path.join(folder, new_name)
    
    if os.path.exists(new_full_path):
        await message.answer("❌ Файл/папка с таким именем уже существует!")
        return
    
    os.rename(old_full_path, new_full_path)
    
    log_action(user_id, "rename", project_name, f"{file_path} -> {new_name}")
    
    await state.clear()
    await message.answer(f"✅ Переименовано в: {new_name}")
    
    # Возвращаемся в проводник
    folder_path = os.path.dirname(file_path)
    await message.answer(
        f"📁 {project_name} - {folder_path if folder_path else 'Корень'}",
        reply_markup=get_explorer_keyboard(project_type, target_user_id, project_name, folder_path)
    )

@dp.callback_query(lambda c: c.data.startswith("delfile_"))
async def delete_file(callback: CallbackQuery):
    _, _, project_type, user_id, project_name, file_path = callback.data.split("_", 5)
    user_id = int(user_id) if user_id != 'None' else None
    
    # Определяем путь
    if project_type == "admin":
        base_path = get_admin_project_path(project_name)
    else:
        base_path = get_user_project_path(user_id, project_name)
    
    full_path = os.path.join(base_path, file_path)
    
    try:
        if os.path.isdir(full_path):
            shutil.rmtree(full_path)
            action = "delete_folder"
        else:
            os.remove(full_path)
            action = "delete_file"
        
        log_action(callback.from_user.id, action, project_name, file_path)
        
        await callback.answer(f"✅ Удалено!")
        
        # Возвращаемся в проводник
        folder_path = os.path.dirname(file_path)
        await callback.message.edit_text(
            f"📁 {project_name} - {folder_path if folder_path else 'Корень'}",
            reply_markup=get_explorer_keyboard(project_type, user_id, project_name, folder_path)
        )
    except Exception as e:
        await callback.answer(f"❌ Ошибка: {e}", show_alert=True)

@dp.callback_query(lambda c: c.data.startswith("getfile_"))
async def download_file(callback: CallbackQuery):
    _, _, project_type, user_id, project_name, file_path = callback.data.split("_", 5)
    user_id = int(user_id) if user_id != 'None' else None
    
    # Определяем путь
    if project_type == "admin":
        base_path = get_admin_project_path(project_name)
    else:
        base_path = get_user_project_path(user_id, project_name)
    
    full_path = os.path.join(base_path, file_path)
    
    if not os.path.exists(full_path):
        await callback.answer("❌ Файл не найден!", show_alert=True)
        return
    
    await callback.answer("⏳ Отправляю файл...")
    
    try:
        await callback.message.answer_document(
            FSInputFile(full_path),
            caption=f"📄 {os.path.basename(file_path)}"
        )
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка: {e}")

@dp.callback_query(lambda c: c.data.startswith("make_main_"))
async def make_main_file(callback: CallbackQuery):
    _, _, project_type, user_id, project_name, file_path = callback.data.split("_", 5)
    user_id = int(user_id) if user_id != 'None' else None
    
    # Определяем путь к проекту
    if project_type == "admin":
        project_path = get_admin_project_path(project_name)
    else:
        project_path = get_user_project_path(user_id, project_name)
    
    # Сохраняем как основной
    set_main_file(project_path, os.path.basename(file_path))
    
    log_action(callback.from_user.id, "set_main_file", project_name, file_path)
    
    await callback.answer(f"✅ Основной файл установлен: {os.path.basename(file_path)}")
    
    # Возвращаемся к файлу
    await file_details(callback)

@dp.callback_query(lambda c: c.data.startswith("logs_"))
async def view_logs(callback: CallbackQuery):
    _, _, project_type, user_id, project_name = callback.data.split("_", 4)
    user_id = int(user_id) if user_id != 'None' else None
    
    # Определяем путь
    if project_type == "admin":
        project_path = get_admin_project_path(project_name)
    else:
        project_path = get_user_project_path(user_id, project_name)
    
    log_file = os.path.join(project_path, "bot.log")
    
    if not os.path.exists(log_file) or os.path.getsize(log_file) == 0:
        await callback.answer("📭 Лог-файл пуст или не найден!", show_alert=True)
        return
    
    await callback.answer("⏳ Загружаю логи...")
    
    # Отправляем файлом
    try:
        # Добавляем информацию о времени
        temp_log = f"/tmp/{project_name}_log_{int(time.time())}.txt"
        shutil.copy2(log_file, temp_log)
        
        await callback.message.answer_document(
            FSInputFile(temp_log),
            caption=f"📋 Логи проекта {project_name}"
        )
        
        os.remove(temp_log)
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка: {e}")

@dp.callback_query(lambda c: c.data.startswith("install_"))
async def install_dependencies(callback: CallbackQuery):
    _, _, project_type, user_id, project_name = callback.data.split("_", 4)
    user_id = int(user_id) if user_id != 'None' else None
    
    # Определяем путь
    if project_type == "admin":
        project_path = get_admin_project_path(project_name)
    else:
        project_path = get_user_project_path(user_id, project_name)
    
    requirements_path = os.path.join(project_path, "requirements.txt")
    
    if not os.path.exists(requirements_path):
        await callback.answer("❌ requirements.txt не найден!", show_alert=True)
        return
    
    await callback.message.edit_text(f"📦 Устанавливаю зависимости...\nЭто может занять время.")
    await callback.answer()
    
    stdout, stderr, code = await run_command(
        [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
        cwd=project_path
    )
    
    log_action(callback.from_user.id, "install_deps", project_name)
    
    if code == 0:
        await callback.message.answer(f"✅ Зависимости установлены!")
    else:
        await callback.message.answer(f"❌ Ошибка:\n```\n{stderr[:1000]}\n```", parse_mode="Markdown")
    
    # Возвращаемся к проекту
    if project_type == "admin":
        await admin_project_details(callback)
    else:
        await user_project_details(callback)

@dp.callback_query(lambda c: c.data.startswith("start_"))
async def start_project(callback: CallbackQuery):
    _, _, project_type, user_id, project_name = callback.data.split("_", 4)
    user_id = int(user_id) if user_id != 'None' else None
    
    # Определяем путь
    if project_type == "admin":
        project_path = get_admin_project_path(project_name)
    else:
        project_path = get_user_project_path(user_id, project_name)
    
    # Проверяем статус
    if get_project_status(project_path) == "running":
        await callback.answer("❌ Проект уже запущен!", show_alert=True)
        return
    
    # Получаем основной файл
    main_file = get_main_file(project_path)
    if not main_file:
        await callback.answer("❌ Не выбран основной файл!", show_alert=True)
        return
    
    main_file_path = os.path.join(project_path, main_file)
    if not os.path.exists(main_file_path):
        await callback.answer(f"❌ Файл {main_file} не найден!", show_alert=True)
        return
    
    await callback.message.edit_text(f"🚀 Запускаю проект...")
    await callback.answer()
    
    # Лог файл
    log_file = os.path.join(project_path, "bot.log")
    
    try:
        # Запускаем процесс
        process = subprocess.Popen(
            [sys.executable, main_file],
            cwd=project_path,
            stdout=open(log_file, 'a'),
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True
        )
        
        # Сохраняем PID
        with open(os.path.join(project_path, ".pid"), 'w') as f:
            f.write(str(process.pid))
        
        # Обновляем статус в БД
        conn = sqlite3.connect(USERS_DB)
        c = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.execute("""UPDATE user_projects SET status = 'running', last_run = ? 
                     WHERE user_id = ? AND project_name = ?""",
                  (now, user_id, project_name))
        conn.commit()
        conn.close()
        
        # Логируем
        with open(log_file, 'a') as f:
            f.write(f"\n--- Запуск {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        
        log_action(callback.from_user.id, "start_project", project_name)
        
        await callback.message.answer(f"✅ Проект запущен (PID: {process.pid})")
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка: {e}")
    
    # Возвращаемся к проекту
    if project_type == "admin":
        await admin_project_details(callback)
    else:
        await user_project_details(callback)

@dp.callback_query(lambda c: c.data.startswith("stop_"))
async def stop_project(callback: CallbackQuery):
    _, _, project_type, user_id, project_name = callback.data.split("_", 4)
    user_id = int(user_id) if user_id != 'None' else None
    
    # Определяем путь
    if project_type == "admin":
        project_path = get_admin_project_path(project_name)
    else:
        project_path = get_user_project_path(user_id, project_name)
    
    pid_file = os.path.join(project_path, ".pid")
    
    if not os.path.exists(pid_file):
        await callback.answer("❌ Проект не запущен!", show_alert=True)
        return
    
    try:
        with open(pid_file, 'r') as f:
            pid = int(f.read().strip())
        
        # Завершаем процесс
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)])
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except:
                os.kill(pid, signal.SIGTERM)
            
            await asyncio.sleep(2)
            
            try:
                os.kill(pid, 0)
                os.kill(pid, signal.SIGKILL)
            except:
                pass
        
        os.remove(pid_file)
        
        # Обновляем статус в БД
        conn = sqlite3.connect(USERS_DB)
        c = conn.cursor()
        c.execute("""UPDATE user_projects SET status = 'stopped' 
                     WHERE user_id = ? AND project_name = ?""",
                  (user_id, project_name))
        conn.commit()
        conn.close()
        
        # Логируем
        log_file = os.path.join(project_path, "bot.log")
        with open(log_file, 'a') as f:
            f.write(f"\n--- Остановлен {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        
        log_action(callback.from_user.id, "stop_project", project_name)
        
        await callback.answer(f"✅ Проект остановлен!")
    except Exception as e:
        await callback.answer(f"❌ Ошибка: {e}", show_alert=True)
    
    # Возвращаемся к проекту
    if project_type == "admin":
        await admin_project_details(callback)
    else:
        await user_project_details(callback)

@dp.callback_query(lambda c: c.data.startswith("restart_"))
async def restart_project(callback: CallbackQuery):
    await stop_project(callback)
    await asyncio.sleep(2)
    await start_project(callback)

@dp.callback_query(lambda c: c.data.startswith("delete_"))
async def delete_project(callback: CallbackQuery):
    _, _, project_type, user_id, project_name = callback.data.split("_", 4)
    user_id = int(user_id) if user_id != 'None' else None
    
    # Определяем путь
    if project_type == "admin":
        project_path = get_admin_project_path(project_name)
    else:
        project_path = get_user_project_path(user_id, project_name)
    
    # Останавливаем если запущен
    if get_project_status(project_path) == "running":
        pid_file = os.path.join(project_path, ".pid")
        try:
            with open(pid_file, 'r') as f:
                pid = int(f.read().strip())
            if os.name == 'nt':
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)])
            else:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except:
                    os.kill(pid, signal.SIGKILL)
        except:
            pass
    
    # Удаляем из БД
    conn = sqlite3.connect(USERS_DB)
    c = conn.cursor()
    c.execute("DELETE FROM user_projects WHERE user_id = ? AND project_name = ?", (user_id, project_name))
    conn.commit()
    conn.close()
    
    # Удаляем папку
    try:
        shutil.rmtree(project_path)
        log_action(callback.from_user.id, "delete_project", project_name)
        await callback.answer(f"✅ Проект удален!")
    except Exception as e:
        await callback.answer(f"❌ Ошибка: {e}", show_alert=True)
    
    # Возвращаемся к списку
    await my_projects(callback)

@dp.callback_query(lambda c: c.data.startswith("download_"))
async def download_project(callback: CallbackQuery):
    _, _, project_type, user_id, project_name = callback.data.split("_", 4)
    user_id = int(user_id) if user_id != 'None' else None
    
    # Определяем путь
    if project_type == "admin":
        project_path = get_admin_project_path(project_name)
    else:
        project_path = get_user_project_path(user_id, project_name)
    
    await callback.answer("⏳ Создаю архив...")
    
    zip_path = f"/tmp/{project_name}_{int(time.time())}.zip"
    try:
        shutil.make_archive(zip_path.replace('.zip', ''), 'zip', project_path)
        
        await callback.message.answer_document(
            FSInputFile(zip_path),
            caption=f"📦 Архив {project_name}"
        )
        
        os.remove(zip_path)
        log_action(callback.from_user.id, "download_project", project_name)
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка: {e}")

# ==================== АДМИН ПАНЕЛЬ ====================
@dp.callback_query(lambda c: c.data == "admin_panel")
async def admin_panel(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    
    await callback.message.edit_text(
        "⚙️ *Админ панель*\n\n"
        "Выбери раздел:",
        parse_mode="Markdown",
        reply_markup=get_admin_panel_keyboard()
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    
    stats = get_all_users_stats()
    
    text = (
        "📊 *Общая статистика*\n\n"
        f"👥 Всего пользователей: `{stats['total_users']}`\n"
        f"👑 Админов: `{stats['total_admins']}`\n"
        f"📁 Всего проектов: `{stats['total_projects']}`\n"
        f"🟢 Запущено сейчас: `{stats['running_projects']}`\n\n"
        
        "*🔥 Топ действий (7 дней):*\n"
    )
    
    for action, count in stats['top_actions']:
        text += f"• {action}: `{count}`\n"
    
    text += "\n*👑 Топ пользователей по проектам:*\n"
    for user_id, count in stats['top_users']:
        text += f"• ID `{user_id}`: `{count}` проектов\n"
    
    await callback.message.edit_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardBuilder().button(text="🔙 Назад", callback_data="admin_panel").as_markup()
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_users")
async def admin_users(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    
    conn = sqlite3.connect(USERS_DB)
    c = conn.cursor()
    c.execute("""SELECT user_id, username, first_name, joined_date, is_admin, total_projects, last_active 
                 FROM users ORDER BY last_active DESC LIMIT 20""")
    users = c.fetchall()
    conn.close()
    
    builder = InlineKeyboardBuilder()
    
    for user in users:
        user_id, username, first_name, joined, is_admin, projects, last_active = user
        name = first_name[:15] if first_name else f"ID:{user_id}"
        status = "👑" if is_admin else "👤"
        active = "🟢" if (datetime.now() - datetime.strptime(last_active, "%Y-%m-%d %H:%M:%S")).seconds < 3600 else "⚪"
        builder.button(
            text=f"{active}{status} {name} ({projects})",
            callback_data=f"admin_user_{user_id}"
        )
    
    builder.button(text="🔙 Назад", callback_data="admin_panel")
    builder.adjust(1)
    
    await callback.message.edit_text(
        "👥 Последние активные пользователи:",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("admin_user_"))
async def admin_user_details(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    
    target_user_id = int(callback.data.replace("admin_user_", ""))
    
    # Получаем информацию
    conn = sqlite3.connect(USERS_DB)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (target_user_id,))
    user = c.fetchone()
    
    c.execute("SELECT project_name, created_date, last_run, status FROM user_projects WHERE user_id = ?", (target_user_id,))
    projects = c.fetchall()
    
    c.execute("SELECT action, project_name, timestamp FROM stats WHERE user_id = ? ORDER BY timestamp DESC LIMIT 10", (target_user_id,))
    actions = c.fetchall()
    conn.close()
    
    if not user:
        await callback.answer("❌ Пользователь не найден!")
        return
    
    text = (
        f"👤 *Информация о пользователе*\n\n"
        f"🆔 ID: `{user[0]}`\n"
        f"📛 Имя: `{user[2] or 'Нет'}`\n"
        f"🏷 Username: @{user[1] if user[1] != 'Нет username' else 'Нет'}\n"
        f"📅 Присоединился: `{user[3]}`\n"
        f"👑 Админ: {'✅' if user[4] else '❌'}\n"
        f"📦 Лимит проектов: `{user[5]}`\n"
        f"📁 Всего проектов: `{user[6]}`\n"
        f"🕐 Последняя активность: `{user[7]}`\n\n"
        
        f"*Проекты ({len(projects)}):*\n"
    )
    
    for p in projects:
        status_emoji = "🟢" if p[3] == "running" else "🔴"
        text += f"{status_emoji} `{p[0]}` (создан: {p[1]})\n"
    
    text += f"\n*Последние действия:*\n"
    for a in actions[:5]:
        text += f"• {a[0]}: {a[1] or '-'} ({a[2]})\n"
    
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад к списку", callback_data="admin_users")
    builder.button(text="⚙️ Управление", callback_data=f"admin_manage_{target_user_id}")
    
    await callback.message.edit_text(
        text,
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_all_projects")
async def admin_all_projects(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    
    conn = sqlite3.connect(USERS_DB)
    c = conn.cursor()
    c.execute("""SELECT user_id, project_name, created_date, last_run, status 
                 FROM user_projects ORDER BY last_run DESC NULLS LAST LIMIT 20""")
    projects = c.fetchall()
    conn.close()
    
    text = "📁 *Все проекты*\n\n"
    builder = InlineKeyboardBuilder()
    
    for p in projects:
        user_id, name, created, last_run, status = p
        status_emoji = "🟢" if status == "running" else "🔴"
        text += f"{status_emoji} `{name}` (ID:{user_id})\n"
        builder.button(
            text=f"📁 {name}",
            callback_data=f"admin_open_project_{user_id}_{name}"
        )
    
    builder.button(text="🔙 Назад", callback_data="admin_panel")
    builder.adjust(1)
    
    await callback.message.edit_text(
        text,
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("admin_open_project_"))
async def admin_open_project(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    
    _, _, _, user_id, project_name = callback.data.split("_", 4)
    user_id = int(user_id)
    
    # Перенаправляем на просмотр проекта
    await user_project_details(callback)

@dp.callback_query(lambda c: c.data == "admin_logs")
async def admin_logs(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    
    conn = sqlite3.connect(USERS_DB)
    c = conn.cursor()
    c.execute("""SELECT user_id, action, project_name, timestamp, details 
                 FROM stats ORDER BY timestamp DESC LIMIT 50""")
    logs = c.fetchall()
    conn.close()
    
    # Создаем файл с логами
    log_text = "=== ЛОГИ ДЕЙСТВИЙ ===\n\n"
    for log in logs:
        user_id, action, project, ts, details = log
        log_text += f"[{ts}] User {user_id}: {action}"
        if project:
            log_text += f" | Project: {project}"
        if details:
            log_text += f" | Details: {details}"
        log_text += "\n"
    
    temp_log = f"/tmp/admin_logs_{int(time.time())}.txt"
    with open(temp_log, 'w', encoding='utf-8') as f:
        f.write(log_text)
    
    await callback.message.answer_document(
        FSInputFile(temp_log),
        caption="📋 Полные логи действий"
    )
    
    os.remove(temp_log)
    await callback.answer()

@dp.message(F.document)
async def handle_zip_project(message: Message, state: FSMContext):
    """Обработка загрузки ZIP как нового проекта"""
    user_id = message.from_user.id
    document = message.document
    
    # Проверяем, не в состоянии ли ожидания
    current_state = await state.get_state()
    if current_state == ProjectStates.waiting_for_file_upload.state:
        # Это загрузка файла в существующий проект
        await handle_file_upload(message, state)
        return
    
    if not document.file_name.endswith('.zip'):
        await message.answer("❌ Пожалуйста, отправь ZIP архив.")
        return
    
    # Проверяем лимиты
    can_create, remaining = can_create_project(user_id)
    if not can_create and user_id not in ADMIN_IDS:
        await message.answer(f"❌ Ты достиг лимита проектов ({MAX_USER_PROJECTS})!")
        return
    
    # Спрашиваем имя проекта
    await state.set_state(ProjectStates.waiting_for_zip)
    await state.update_data(file_id=document.file_id, file_name=document.file_name)
    
    await message.answer(
        "📝 Введи название для проекта (или отправь /cancel для отмены):"
    )

@dp.message(ProjectStates.waiting_for_zip)
async def save_zip_project(message: Message, state: FSMContext):
    user_id = message.from_user.id
    project_name = message.text.strip()
    
    # Проверяем имя
    if not project_name.replace('_', '').replace('-', '').isalnum():
        await message.answer("❌ Имя может содержать только буквы, цифры, _ и -")
        return
    
    data = await state.get_data()
    
    if user_id in ADMIN_IDS:
        project_path = get_admin_project_path(project_name)
    else:
        project_path = get_user_project_path(user_id, project_name)
    
    if os.path.exists(project_path):
        await message.answer("❌ Проект с таким именем уже существует!")
        return
    
    # Скачиваем и распаковываем
    file = await bot.get_file(data['file_id'])
    zip_path = f"/tmp/{data['file_name']}"
    await bot.download_file(file.file_path, zip_path)
    
    try:
        os.makedirs(project_path, exist_ok=True)
        
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(project_path)
        
        os.remove(zip_path)
        
        # Сохраняем в БД
        conn = sqlite3.connect(USERS_DB)
        c = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.execute("""INSERT INTO user_projects (user_id, project_name, created_date, status)
                     VALUES (?, ?, ?, ?)""", (user_id, project_name, now, "stopped"))
        c.execute("UPDATE users SET total_projects = total_projects + 1 WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        
        log_action(user_id, "upload_zip", project_name)
        
        await message.answer(f"✅ Проект '{project_name}' успешно загружен!")
        
        # Проверяем Python файлы
        py_files = [f for f in os.listdir(project_path) if f.endswith('.py')]
        if py_files:
            if len(py_files) == 1:
                set_main_file(project_path, py_files[0])
                await message.answer(f"⚙️ Основной файл установлен: {py_files[0]}")
        
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        if os.path.exists(project_path):
            shutil.rmtree(project_path)
    
    await state.clear()

@dp.message(Command("cancel"))
async def cancel_operation(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("❌ Нет активной операции.")
        return
    
    await state.clear()
    await message.answer("✅ Операция отменена.")

# ==================== ЗАПУСК ====================
async def main():
    # Инициализируем БД
    init_database()
    
    # Создаем админскую папку
    os.makedirs(get_admin_project_path(), exist_ok=True)
    
    print("🚀 Хост-бот запущен!")
    print(f"📁 Папка проектов: {os.path.abspath(PROJECTS_DIR)}")
    print(f"👑 Админы: {ADMIN_IDS}")
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())