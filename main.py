import asyncio
import zipfile
import os
import subprocess
import sys
import signal
import time
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
import shutil
from pathlib import Path
import psutil  # Для мониторинга процессов

# Конфигурация
BOT_TOKEN = "7702565826:AAE-s3_TdJazx2mV9BPEFPbMUsr-QZY3WfU"
ADMIN_ID = 6945488830  # Твой Telegram ID

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Константы
PROJECTS_DIR = "projects"
os.makedirs(PROJECTS_DIR, exist_ok=True)

# Состояния для FSM
class ProjectStates(StatesGroup):
    waiting_for_main_file = State()
    waiting_for_file_to_edit = State()
    waiting_for_file_content = State()
    waiting_for_new_file_name = State()

# --- Вспомогательные функции ---
def get_projects_list():
    """Возвращает список папок (проектов) в директории projects"""
    if not os.path.exists(PROJECTS_DIR):
        return []
    return [d for d in os.listdir(PROJECTS_DIR) 
            if os.path.isdir(os.path.join(PROJECTS_DIR, d))]

def get_project_keyboard():
    """Создает инлайн клавиатуру со списком проектов"""
    builder = InlineKeyboardBuilder()
    projects = get_projects_list()
    
    if not projects:
        builder.button(text="📂 Нет проектов", callback_data="no_projects")
    else:
        for project in projects:
            # Проверяем статус проекта (запущен/остановлен)
            status = get_project_status(project)
            status_emoji = "🟢" if status == "running" else "🔴"
            builder.button(text=f"{status_emoji} {project}", callback_data=f"project_{project}")
    
    builder.button(text="➕ Загрузить новый проект", callback_data="upload_project")
    builder.button(text="🔄 Обновить список", callback_data="refresh_projects")
    builder.adjust(1)
    return builder.as_markup()

def get_project_status(project_name: str):
    """Проверяет, запущен ли проект"""
    project_path = os.path.join(PROJECTS_DIR, project_name)
    pid_file = os.path.join(project_path, ".pid")
    
    if not os.path.exists(pid_file):
        return "stopped"
    
    try:
        with open(pid_file, 'r') as f:
            pid = int(f.read().strip())
        
        # Проверяем, существует ли процесс
        process = psutil.Process(pid)
        if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
            return "running"
        else:
            # Процесс не существует, удаляем pid файл
            os.remove(pid_file)
            return "stopped"
    except:
        # Ошибка при чтении или процессе не существует
        if os.path.exists(pid_file):
            os.remove(pid_file)
        return "stopped"

def get_project_actions_keyboard(project_name: str):
    """Клавиатура действий для конкретного проекта"""
    builder = InlineKeyboardBuilder()
    status = get_project_status(project_name)
    
    if status == "running":
        builder.button(text="⏹ Остановить", callback_data=f"stop_{project_name}")
        builder.button(text="🔄 Перезапустить", callback_data=f"restart_{project_name}")
    else:
        builder.button(text="▶️ Запустить", callback_data=f"start_{project_name}")
    
    builder.button(text="📦 Установить зависимости", callback_data=f"install_{project_name}")
    builder.button(text="📁 Управление файлами", callback_data=f"files_{project_name}")
    builder.button(text="📋 Посмотреть логи", callback_data=f"logs_{project_name}")
    builder.button(text="⚙️ Выбрать основной файл", callback_data=f"setmain_{project_name}")
    builder.button(text="📥 Скачать проект", callback_data=f"download_{project_name}")
    builder.button(text="🗑 Удалить проект", callback_data=f"delete_{project_name}")
    builder.button(text="🔙 Назад к списку", callback_data="back_to_projects")
    builder.adjust(2)
    return builder.as_markup()

def get_files_keyboard(project_name: str, path: str = ""):
    """Клавиатура для навигации по файлам проекта"""
    builder = InlineKeyboardBuilder()
    project_path = os.path.join(PROJECTS_DIR, project_name)
    current_path = os.path.join(project_path, path) if path else project_path
    
    # Кнопка "Назад" если не в корне
    if path:
        parent_path = os.path.dirname(path)
        builder.button(text="📂 ..", callback_data=f"folder_{project_name}_{parent_path}")
    
    # Сортируем содержимое: папки, потом файлы
    items = sorted(os.listdir(current_path))
    folders = [i for i in items if os.path.isdir(os.path.join(current_path, i))]
    files = [i for i in items if os.path.isfile(os.path.join(current_path, i)) and not i.startswith('.')]
    
    for folder in folders:
        new_path = os.path.join(path, folder) if path else folder
        builder.button(text=f"📁 {folder}/", callback_data=f"folder_{project_name}_{new_path}")
    
    for file in files:
        file_path = os.path.join(path, file) if path else file
        builder.button(text=f"📄 {file}", callback_data=f"file_{project_name}_{file_path}")
    
    builder.button(text="➕ Создать файл", callback_data=f"newfile_{project_name}_{path}")
    builder.button(text="🔙 Назад к проекту", callback_data=f"project_{project_name}")
    builder.adjust(1)
    return builder.as_markup()

def get_file_actions_keyboard(project_name: str, file_path: str):
    """Клавиатура действий с файлом"""
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Редактировать", callback_data=f"edit_{project_name}_{file_path}")
    builder.button(text="📥 Скачать", callback_data=f"downfile_{project_name}_{file_path}")
    builder.button(text="🗑 Удалить", callback_data=f"delfile_{project_name}_{file_path}")
    builder.button(text="🔙 Назад", callback_data=f"folder_{project_name}_{os.path.dirname(file_path)}")
    builder.adjust(2)
    return builder.as_markup()

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

def get_main_file(project_path: str):
    """Определяет основной файл проекта из конфига или ищет стандартные"""
    config_file = os.path.join(project_path, ".bot_config")
    if os.path.exists(config_file):
        with open(config_file, 'r') as f:
            return f.read().strip()
    
    # Стандартные варианты
    for file in ["bot.py", "main.py", "app.py", "run.py"]:
        if os.path.exists(os.path.join(project_path, file)):
            return file
    return None

def set_main_file(project_path: str, filename: str):
    """Сохраняет основной файл в конфиг"""
    config_file = os.path.join(project_path, ".bot_config")
    with open(config_file, 'w') as f:
        f.write(filename)

# --- Обработчики ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Доступ запрещен.")
        return
    
    # Проверяем статусы всех проектов при старте
    projects = get_projects_list()
    status_messages = []
    for project in projects:
        status = get_project_status(project)
        if status == "running":
            status_messages.append(f"🟢 {project}")
        else:
            status_messages.append(f"🔴 {project}")
    
    status_text = "\n".join(status_messages) if status_messages else "Нет проектов"
    
    await message.answer(
        f"👋 Добро пожаловать в Хост-бот!\n\n"
        f"📊 Статус проектов:\n{status_text}\n\n"
        f"Выбери проект из списка:",
        reply_markup=get_project_keyboard()
    )

@dp.callback_query(lambda c: c.data == "back_to_projects")
async def back_to_projects(callback: CallbackQuery):
    await callback.message.edit_text(
        "👋 Выбери проект из списка:",
        reply_markup=get_project_keyboard()
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "refresh_projects")
async def refresh_projects(callback: CallbackQuery):
    await callback.message.edit_text(
        "👋 Список проектов обновлен:",
        reply_markup=get_project_keyboard()
    )
    await callback.answer("✅ Список обновлен!")

@dp.callback_query(lambda c: c.data == "upload_project")
async def upload_project_prompt(callback: CallbackQuery):
    await callback.message.edit_text(
        "📤 Отправь ZIP-архив с твоим проектом.\n"
        "Убедись, что в корне архива есть файл `requirements.txt` и основной файл (например, `bot.py`)."
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("project_"))
async def project_details(callback: CallbackQuery):
    project_name = callback.data.replace("project_", "")
    project_path = os.path.join(PROJECTS_DIR, project_name)
    
    if not os.path.exists(project_path):
        await callback.answer("❌ Проект не найден!", show_alert=True)
        await back_to_projects(callback)
        return
    
    # Считаем размер папки
    total_size = 0
    file_count = 0
    for dirpath, dirnames, filenames in os.walk(project_path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            total_size += os.path.getsize(fp)
            file_count += 1
    
    size_mb = total_size / (1024 * 1024)
    status = get_project_status(project_name)
    status_emoji = "🟢" if status == "running" else "🔴"
    main_file = get_main_file(project_path) or "Не выбран"
    
    # Проверяем время последнего изменения
    last_modified = datetime.fromtimestamp(os.path.getmtime(project_path)).strftime("%Y-%m-%d %H:%M:%S")
    
    text = (
        f"📁 *Проект: {project_name}*\n"
        f"{status_emoji} Статус: `{status}`\n"
        f"📦 Размер: `{size_mb:.2f} MB`\n"
        f"📄 Файлов: `{file_count}`\n"
        f"⚙️ Основной файл: `{main_file}`\n"
        f"🕐 Изменен: `{last_modified}`\n\n"
        "Выбери действие:"
    )
    
    await callback.message.edit_text(
        text,
        reply_markup=get_project_actions_keyboard(project_name),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("files_"))
async def manage_files(callback: CallbackQuery):
    project_name = callback.data.replace("files_", "")
    await callback.message.edit_text(
        f"📁 Файлы проекта '{project_name}':",
        reply_markup=get_files_keyboard(project_name)
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("folder_"))
async def open_folder(callback: CallbackQuery):
    _, project_name, path = callback.data.split("_", 2)
    await callback.message.edit_text(
        f"📁 {path if path else 'Корневая папка'}:",
        reply_markup=get_files_keyboard(project_name, path)
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("file_"))
async def open_file(callback: CallbackQuery):
    _, project_name, file_path = callback.data.split("_", 2)
    full_path = os.path.join(PROJECTS_DIR, project_name, file_path)
    
    if not os.path.exists(full_path):
        await callback.answer("❌ Файл не найден!", show_alert=True)
        return
    
    # Показываем информацию о файле
    size = os.path.getsize(full_path)
    size_kb = size / 1024
    last_modified = datetime.fromtimestamp(os.path.getmtime(full_path)).strftime("%Y-%m-%d %H:%M:%S")
    
    text = (
        f"📄 *Файл: {os.path.basename(file_path)}*\n"
        f"📦 Размер: `{size_kb:.2f} KB`\n"
        f"🕐 Изменен: `{last_modified}`\n"
        f"📍 Путь: `{file_path}`\n\n"
        f"Выбери действие:"
    )
    
    await callback.message.edit_text(
        text,
        reply_markup=get_file_actions_keyboard(project_name, file_path),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("edit_"))
async def edit_file(callback: CallbackQuery, state: FSMContext):
    _, project_name, file_path = callback.data.split("_", 2)
    full_path = os.path.join(PROJECTS_DIR, project_name, file_path)
    
    if not os.path.exists(full_path):
        await callback.answer("❌ Файл не найден!", show_alert=True)
        return
    
    # Читаем содержимое файла
    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except UnicodeDecodeError:
        await callback.answer("❌ Невозможно отредактировать бинарный файл!", show_alert=True)
        return
    
    # Сохраняем состояние
    await state.set_state(ProjectStates.waiting_for_file_content)
    await state.update_data(project_name=project_name, file_path=file_path)
    
    # Отправляем текущее содержимое
    if len(content) > 3500:
        # Если файл большой, отправляем частями
        parts = [content[i:i+3500] for i in range(0, len(content), 3500)]
        for i, part in enumerate(parts):
            await callback.message.answer(f"Часть {i+1}:\n```\n{part}\n```", parse_mode="Markdown")
    else:
        await callback.message.answer(f"Текущее содержимое:\n```\n{content}\n```", parse_mode="Markdown")
    
    await callback.message.answer(
        "✏️ Отправь новое содержимое файла (текст сообщения).\n"
        "Для отмены отправь /cancel"
    )
    await callback.answer()

@dp.message(ProjectStates.waiting_for_file_content)
async def save_file_content(message: Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Редактирование отменено.")
        return
    
    data = await state.get_data()
    project_name = data['project_name']
    file_path = data['file_path']
    full_path = os.path.join(PROJECTS_DIR, project_name, file_path)
    
    try:
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(message.text)
        
        await message.answer(f"✅ Файл успешно сохранен!")
        await state.clear()
        
        # Возвращаемся к файлу
        await message.answer(
            f"📄 {os.path.basename(file_path)}",
            reply_markup=get_file_actions_keyboard(project_name, file_path)
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка при сохранении: {e}")

@dp.callback_query(lambda c: c.data.startswith("newfile_"))
async def new_file_prompt(callback: CallbackQuery, state: FSMContext):
    _, project_name, path = callback.data.split("_", 2)
    await state.set_state(ProjectStates.waiting_for_new_file_name)
    await state.update_data(project_name=project_name, path=path)
    
    await callback.message.edit_text(
        f"📝 Введи имя нового файла (с расширением, например script.py):\n"
        f"Папка: {path if path else 'Корневая'}"
    )
    await callback.answer()

@dp.message(ProjectStates.waiting_for_new_file_name)
async def create_new_file(message: Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Создание отменено.")
        return
    
    data = await state.get_data()
    project_name = data['project_name']
    path = data['path']
    filename = message.text.strip()
    
    # Проверяем имя файла
    if not filename or '/' in filename or '\\' in filename:
        await message.answer("❌ Некорректное имя файла. Попробуй еще раз или отправь /cancel")
        return
    
    full_path = os.path.join(PROJECTS_DIR, project_name, path, filename)
    
    if os.path.exists(full_path):
        await message.answer("❌ Файл уже существует! Попробуй другое имя или отправь /cancel")
        return
    
    try:
        # Создаем пустой файл
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write("# Новый файл\n")
        
        await message.answer(f"✅ Файл {filename} создан!")
        await state.clear()
        
        # Показываем обновленную папку
        await message.answer(
            f"📁 {path if path else 'Корневая папка'}:",
            reply_markup=get_files_keyboard(project_name, path)
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка при создании: {e}")

@dp.callback_query(lambda c: c.data.startswith("delfile_"))
async def delete_file(callback: CallbackQuery):
    _, project_name, file_path = callback.data.split("_", 2)
    full_path = os.path.join(PROJECTS_DIR, project_name, file_path)
    
    try:
        os.remove(full_path)
        await callback.answer(f"✅ Файл удален!")
        
        # Возвращаемся в папку
        folder_path = os.path.dirname(file_path)
        await callback.message.edit_text(
            f"📁 {folder_path if folder_path else 'Корневая папка'}:",
            reply_markup=get_files_keyboard(project_name, folder_path)
        )
    except Exception as e:
        await callback.answer(f"❌ Ошибка удаления: {e}", show_alert=True)

@dp.callback_query(lambda c: c.data.startswith("downfile_"))
async def download_file(callback: CallbackQuery):
    _, project_name, file_path = callback.data.split("_", 2)
    full_path = os.path.join(PROJECTS_DIR, project_name, file_path)
    
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
        await callback.message.answer(f"❌ Ошибка отправки: {e}")

@dp.callback_query(lambda c: c.data.startswith("logs_"))
async def view_logs(callback: CallbackQuery):
    project_name = callback.data.replace("logs_", "")
    project_path = os.path.join(PROJECTS_DIR, project_name)
    log_file = os.path.join(project_path, "bot.log")
    
    if not os.path.exists(log_file):
        await callback.answer("❌ Лог-файл не найден!", show_alert=True)
        return
    
    # Читаем последние 50 строк лога
    try:
        with open(log_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            last_lines = lines[-50:] if len(lines) > 50 else lines
        
        log_text = ''.join(last_lines)
        
        if len(log_text) > 4000:
            # Если лог большой, отправляем файлом
            temp_log = f"/tmp/{project_name}_log.txt"
            with open(temp_log, 'w', encoding='utf-8') as f:
                f.write(log_text)
            
            await callback.message.answer_document(
                FSInputFile(temp_log),
                caption=f"📋 Лог проекта {project_name} (последние {len(last_lines)} строк)"
            )
            os.remove(temp_log)
        else:
            await callback.message.answer(
                f"📋 Последние строки лога:\n```\n{log_text}\n```",
                parse_mode="Markdown"
            )
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка чтения лога: {e}")
    
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("setmain_"))
async def set_main_file_prompt(callback: CallbackQuery, state: FSMContext):
    project_name = callback.data.replace("setmain_", "")
    project_path = os.path.join(PROJECTS_DIR, project_name)
    
    # Собираем все Python файлы
    py_files = []
    for file in os.listdir(project_path):
        if file.endswith('.py') and os.path.isfile(os.path.join(project_path, file)):
            py_files.append(file)
    
    if not py_files:
        await callback.answer("❌ Нет Python файлов в проекте!", show_alert=True)
        return
    
    # Создаем клавиатуру с файлами
    builder = InlineKeyboardBuilder()
    for file in py_files:
        builder.button(text=file, callback_data=f"selectmain_{project_name}_{file}")
    builder.button(text="🔙 Назад", callback_data=f"project_{project_name}")
    builder.adjust(1)
    
    await callback.message.edit_text(
        f"⚙️ Выбери основной файл для проекта '{project_name}':",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("selectmain_"))
async def set_main_file(callback: CallbackQuery):
    _, project_name, filename = callback.data.split("_", 2)
    project_path = os.path.join(PROJECTS_DIR, project_name)
    
    set_main_file(project_path, filename)
    await callback.answer(f"✅ Основной файл установлен: {filename}")
    
    # Возвращаемся к проекту
    await project_details(callback)

@dp.callback_query(lambda c: c.data.startswith("install_"))
async def install_dependencies(callback: CallbackQuery):
    project_name = callback.data.replace("install_", "")
    project_path = os.path.join(PROJECTS_DIR, project_name)
    requirements_path = os.path.join(project_path, "requirements.txt")
    
    if not os.path.exists(requirements_path):
        await callback.answer("❌ requirements.txt не найден!", show_alert=True)
        return
    
    await callback.message.edit_text(f"📦 Устанавливаю зависимости для '{project_name}'...\nЭто может занять время.")
    await callback.answer()
    
    stdout, stderr, code = await run_command(
        [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
        cwd=project_path
    )
    
    if code == 0:
        await callback.message.answer(f"✅ Зависимости для '{project_name}' успешно установлены!")
    else:
        await callback.message.answer(f"❌ Ошибка при установке зависимостей:\n```\n{stderr[:1000]}\n```", parse_mode="Markdown")
    
    await project_details(callback)

@dp.callback_query(lambda c: c.data.startswith("start_"))
async def start_project(callback: CallbackQuery):
    project_name = callback.data.replace("start_", "")
    project_path = os.path.join(PROJECTS_DIR, project_name)
    
    # Проверяем, не запущен ли уже
    if get_project_status(project_name) == "running":
        await callback.answer("❌ Проект уже запущен!", show_alert=True)
        return
    
    # Получаем основной файл
    main_file = get_main_file(project_path)
    if not main_file:
        await callback.answer("❌ Не выбран основной файл! Сначала выбери через меню.", show_alert=True)
        return
    
    main_file_path = os.path.join(project_path, main_file)
    if not os.path.exists(main_file_path):
        await callback.answer(f"❌ Файл {main_file} не найден!", show_alert=True)
        return
    
    await callback.message.edit_text(f"🚀 Запускаю '{project_name}'...")
    await callback.answer()
    
    # Создаем лог-файл
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
        
        # Добавляем запись в лог о запуске
        with open(log_file, 'a') as f:
            f.write(f"\n--- Запуск {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        
        await callback.message.answer(f"✅ Проект '{project_name}' запущен (PID: {process.pid})")
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка запуска: {e}")
    
    await project_details(callback)

@dp.callback_query(lambda c: c.data.startswith("stop_"))
async def stop_project(callback: CallbackQuery):
    project_name = callback.data.replace("stop_", "")
    project_path = os.path.join(PROJECTS_DIR, project_name)
    pid_file = os.path.join(project_path, ".pid")
    
    if not os.path.exists(pid_file):
        await callback.answer("❌ Проект не запущен!", show_alert=True)
        return
    
    try:
        with open(pid_file, 'r') as f:
            pid = int(f.read().strip())
        
        # Завершаем процесс и все дочерние процессы
        if os.name == 'nt':  # Windows
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)])
        else:  # Linux/Mac
            # Отправляем SIGTERM всей группе процессов
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except:
                # Если не получилось с группой, убиваем процесс
                os.kill(pid, signal.SIGTERM)
            
            # Даем время на завершение
            await asyncio.sleep(2)
            
            # Если еще висит, принудительно
            try:
                os.kill(pid, 0)  # Проверяем, жив ли
                os.kill(pid, signal.SIGKILL)  # Убиваем принудительно
            except:
                pass  # Процесс уже умер
        
        os.remove(pid_file)
        
        # Добавляем запись в лог
        log_file = os.path.join(project_path, "bot.log")
        with open(log_file, 'a') as f:
            f.write(f"\n--- Остановлен {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        
        await callback.answer(f"✅ Проект '{project_name}' остановлен!")
    except Exception as e:
        await callback.answer(f"❌ Ошибка остановки: {e}", show_alert=True)
    
    await project_details(callback)

@dp.callback_query(lambda c: c.data.startswith("restart_"))
async def restart_project(callback: CallbackQuery):
    project_name = callback.data.replace("restart_", "")
    
    # Сначала останавливаем
    await stop_project(callback)
    await asyncio.sleep(2)
    
    # Потом запускаем
    await start_project(callback)

@dp.callback_query(lambda c: c.data.startswith("delete_"))
async def delete_project(callback: CallbackQuery):
    project_name = callback.data.replace("delete_", "")
    project_path = os.path.join(PROJECTS_DIR, project_name)
    
    # Останавливаем, если запущен
    if get_project_status(project_name) == "running":
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
    
    # Удаляем папку
    try:
        shutil.rmtree(project_path)
        await callback.answer(f"✅ Проект '{project_name}' удален!")
    except Exception as e:
        await callback.answer(f"❌ Ошибка удаления: {e}", show_alert=True)
    
    await back_to_projects(callback)

@dp.callback_query(lambda c: c.data.startswith("download_"))
async def download_project(callback: CallbackQuery):
    project_name = callback.data.replace("download_", "")
    project_path = os.path.join(PROJECTS_DIR, project_name)
    
    if not os.path.exists(project_path):
        await callback.answer("❌ Проект не найден!", show_alert=True)
        return
    
    await callback.answer("⏳ Создаю архив...")
    
    # Создаем ZIP архив
    zip_path = f"/tmp/{project_name}.zip"
    try:
        shutil.make_archive(zip_path.replace('.zip', ''), 'zip', project_path)
        
        # Отправляем файл
        await callback.message.answer_document(
            FSInputFile(zip_path),
            caption=f"📦 Архив проекта {project_name}"
        )
        
        # Удаляем временный архив
        os.remove(zip_path)
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка создания архива: {e}")

@dp.message(F.document)
async def handle_zip_upload(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    document = message.document
    if not document.file_name.endswith('.zip'):
        await message.answer("❌ Пожалуйста, отправь именно ZIP-архив.")
        return
    
    # Скачиваем файл
    file = await bot.get_file(document.file_id)
    zip_path = f"/tmp/{document.file_name}"
    await bot.download_file(file.file_path, zip_path)
    
    # Создаем папку для проекта
    project_name = document.file_name.replace('.zip', '')
    project_path = os.path.join(PROJECTS_DIR, project_name)
    
    # Если папка существует, спрашиваем что делать
    if os.path.exists(project_path):
        # Пока просто перезаписываем
        shutil.rmtree(project_path)
    
    os.makedirs(project_path, exist_ok=True)
    
    # Распаковываем архив
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(project_path)
        
        os.remove(zip_path)
        
        await message.answer(f"✅ Проект '{project_name}' успешно загружен и распакован!")
        
        # Проверяем наличие requirements.txt
        requirements_path = os.path.join(project_path, "requirements.txt")
        if os.path.exists(requirements_path):
            await message.answer("📦 Найден файл requirements.txt. Рекомендую установить зависимости.")
        
        # Проверяем Python файлы
        py_files = [f for f in os.listdir(project_path) if f.endswith('.py')]
        if py_files:
            if len(py_files) == 1:
                # Если только один py файл, делаем его основным
                set_main_file(project_path, py_files[0])
                await message.answer(f"⚙️ Основной файл установлен: {py_files[0]}")
            else:
                await message.answer("⚠️ Найдено несколько Python файлов. Выбери основной в меню проекта.")
        else:
            await message.answer("⚠️ Не найдено Python файлов!")
        
        # Показываем обновленный список
        await cmd_start(message)
        
    except Exception as e:
        await message.answer(f"❌ Ошибка при распаковке: {e}")

# Запуск бота
async def main():
    print("🚀 Хост-бот запущен...")
    print(f"📁 Папка проектов: {os.path.abspath(PROJECTS_DIR)}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())