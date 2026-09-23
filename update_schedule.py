import os
import json
import sqlite3
import logging
import requests
import subprocess
from datetime import datetime
from icalendar import Calendar, Event

DB_FILE = "schedule_cache.db"
ICS_FILE = "itmo_schedule.ics"

# Эндпоинты API ИТМО и Keycloak
TOKEN_URL = "https://id.itmo.ru/auth/realms/itmo/protocol/openid-connect/token"
# Основной и резервные эндпоинты расписания
API_URLS = [
    "https://my.itmo.ru/api/schedule/schedule/personal",
    "https://api.schedule.itmo.su/api/v1/person/schedule/schedule",
]
CLIENT_ID = "student-personal-cabinet"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://my.itmo.ru",
    "Referer": "https://my.itmo.ru/",
}

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


def get_fresh_access_token() -> str:
    """Получает Access Token через Refresh Token."""
    refresh_token = os.getenv("ITMO_REFRESH_TOKEN")
    if not refresh_token:
        logging.error("Переменная окружения ITMO_REFRESH_TOKEN не найдена!")
        return None

    payload = {
        "client_id": CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token.strip(),
    }

    try:
        response = requests.post(TOKEN_URL, data=payload, timeout=10)
        response.raise_for_status()
        data = response.json()

        new_access_token = data.get("access_token")
        new_refresh_token = data.get("refresh_token")

        # Пробуем обновить секрет через GH CLI, если есть права (PAT)
        if new_refresh_token and os.getenv("GITHUB_ACTIONS") == "true":
            try:
                result = subprocess.run(
                    ["gh", "secret", "set", "ITMO_REFRESH_TOKEN", "--body", new_refresh_token],
                    capture_output=True,
                    text=True
                )
                if result.returncode == 0:
                    logging.info("Секрет ITMO_REFRESH_TOKEN успешно обновлен в GitHub Secrets!")
                else:
                    logging.info("Пропуск обновления GitHub Secret (стандартный GITHUB_TOKEN не имеет прав 'secrets', это нормально).")
            except Exception as err:
                logging.debug(f"GH CLI skipped: {err}")

        return new_access_token

    except Exception as e:
        logging.error(f"Не удалось обновить токен через Keycloak: {e}")
        return None


class ScheduleManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS lessons (
                    pair_id INTEGER PRIMARY KEY,
                    subject TEXT,
                    work_type TEXT,
                    start_dt TEXT,
                    end_dt TEXT,
                    location TEXT,
                    teacher TEXT,
                    zoom_url TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            )
            conn.commit()

    def sync_lessons(self, lessons_data: list) -> bool:
        has_changes = False
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            for lesson in lessons_data:
                pair_id = lesson.get("pair_id") or lesson.get("id")
                if not pair_id:
                    continue

                subject = lesson.get("subject") or lesson.get("title") or "Занятие"
                work_type = lesson.get("work_type") or lesson.get("type") or ""
                date_str = lesson.get("date")
                start_time = lesson.get("time_start") or lesson.get("start_time")
                end_time = lesson.get("time_end") or lesson.get("end_time")

                if not (date_str and start_time and end_time):
                    continue

                start_dt = f"{date_str} {start_time}"
                end_dt = f"{date_str} {end_time}"

                building = lesson.get("building") or ""
                room = lesson.get("room") or ""
                location = (
                    f"{building}, ауд. {room}".strip(", ")
                    if building or room
                    else lesson.get("format", "Дистанционно")
                )

                teacher = lesson.get("teacher_name") or lesson.get("teacher") or ""
                zoom_url = lesson.get("zoom_url") or lesson.get("link") or ""

                cursor.execute(
                    "SELECT subject, start_dt, end_dt, location, zoom_url FROM lessons WHERE pair_id = ?",
                    (pair_id,),
                )
                row = cursor.fetchone()
                new_data = (subject, start_dt, end_dt, location, zoom_url)

                if row is None:
                    cursor.execute(
                        """
                        INSERT INTO lessons (pair_id, subject, work_type, start_dt, end_dt, location, teacher, zoom_url)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            pair_id,
                            subject,
                            work_type,
                            start_dt,
                            end_dt,
                            location,
                            teacher,
                            zoom_url,
                        ),
                    )
                    has_changes = True
                elif row != new_data:
                    cursor.execute(
                        """
                        UPDATE lessons 
                        SET subject = ?, work_type = ?, start_dt = ?, end_dt = ?, location = ?, teacher = ?, zoom_url = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE pair_id = ?
                    """,
                        (
                            subject,
                            work_type,
                            start_dt,
                            end_dt,
                            location,
                            teacher,
                            zoom_url,
                            pair_id,
                        ),
                    )
                    has_changes = True

            conn.commit()
        return has_changes

    def generate_ics(self, output_filename: str):
        cal = Calendar()
        cal.add("prodid", "-//ITMO Schedule Auto-Sync//ru//")
        cal.add("version", "2.0")
        cal.add("X-WR-CALNAME", "Расписание ИТМО")

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT pair_id, subject, work_type, start_dt, end_dt, location, teacher, zoom_url FROM lessons"
            )
            rows = cursor.fetchall()

            for row in rows:
                (
                    pair_id,
                    subject,
                    work_type,
                    start_dt_str,
                    end_dt_str,
                    location,
                    teacher,
                    zoom_url,
                ) = row

                try:
                    dt_start = datetime.strptime(start_dt_str, "%Y-%m-%d %H:%M")
                    dt_end = datetime.strptime(end_dt_str, "%Y-%m-%d %H:%M")
                except ValueError:
                    continue

                event = Event()
                event.add(
                    "summary",
                    f"{subject} ({work_type})" if work_type else subject,
                )
                event.add("dtstart", dt_start)
                event.add("dtend", dt_end)
                event.add("location", location)

                desc = []
                if teacher:
                    desc.append(f"Преподаватель: {teacher}")
                if zoom_url:
                    desc.append(f"Ссылка: {zoom_url}")
                event.add("description", "\n".join(desc))

                event.add("uid", f"pair-{pair_id}@itmo.ru")
                cal.add_component(event)

        with open(output_filename, "wb") as f:
            f.write(cal.to_ical())


def fetch_data_from_api() -> list:
    access_token = get_fresh_access_token()
    raw_json = None

    if access_token:
        logging.info("Инициализация запроса к API ИТМО...")
        headers = {
            **HEADERS,
            "Authorization": f"Bearer {access_token}",
        }

        for url in API_URLS:
            try:
                logging.info(f"Пробуем эндпоинт: {url}")
                response = requests.get(url, headers=headers, timeout=15)
                if response.status_code == 200:
                    raw_json = response.json()
                    logging.info("Данные успешно получены из API ИТМО!")
                    break
                else:
                    logging.warning(f"Эндпоинт {url} вернул статус {response.status_code}")
            except Exception as e:
                logging.error(f"Ошибка при запросе к {url}: {e}")

    if not raw_json and os.path.exists("json_data.json"):
        logging.info("Загрузка локального файла json_data.json...")
        with open("json_data.json", "r", encoding="utf-8") as f:
            raw_json = json.load(f)

    if not raw_json:
        return []

    flat_lessons = []
    # Парсим ответ от my.itmo.ru API
    data_content = raw_json.get("data") if isinstance(raw_json, dict) else raw_json

    if isinstance(data_content, list):
        for day in data_content:
            date_str = day.get("date")
            for lesson in day.get("lessons", []):
                lesson["date"] = date_str
                flat_lessons.append(lesson)
    elif isinstance(data_content, dict):
        # Если структура ответа представляет собой объект с днями
        for date_str, lessons in data_content.items():
            if isinstance(lessons, list):
                for lesson in lessons:
                    lesson["date"] = date_str
                    flat_lessons.append(lesson)

    return flat_lessons


def main():
    manager = ScheduleManager(DB_FILE)
    lessons = fetch_data_from_api()

    if not lessons:
        logging.warning("Нет данных для синхронизации.")
        return

    has_changes = manager.sync_lessons(lessons)

    if has_changes or not os.path.exists(ICS_FILE):
        manager.generate_ics(ICS_FILE)
        logging.info("Календарь и база данных успешно обновлены.")


if __name__ == "__main__":
    main()
