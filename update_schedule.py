import argparse
from datetime import datetime, timedelta
import json
import logging
import os
import sqlite3
import subprocess
from icalendar import Calendar, Event, Alarm
import requests

DB_FILE = "schedule_cache.db"
ICS_FILE = "itmo_schedule.ics"

TOKEN_URL = "https://id.itmo.ru/auth/realms/itmo/protocol/openid-connect/token"
CLIENT_ID = "student-personal-cabinet"

API_URLS = [
    "https://my.itmo.ru/api/schedule/schedule/personal",
    "https://api.schedule.itmo.su/api/v1/person/schedule/schedule",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://my.itmo.ru",
    "Referer": "https://my.itmo.ru/",
}

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


def _save_new_refresh_token(new_refresh_token: str):
    """Обновляет секрет ITMO_REFRESH_TOKEN в GitHub Secrets через gh CLI."""
    gh_token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if not gh_token:
        logging.warning(
            "GH_TOKEN не обнаружен. Автоматическое обновление секрета в GitHub пропущено."
        )
        return

    try:
        env = os.environ.copy()
        env["GH_TOKEN"] = gh_token
        result = subprocess.run(
            [
                "gh",
                "secret",
                "set",
                "ITMO_REFRESH_TOKEN",
                "--body",
                new_refresh_token.strip(),
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode == 0:
            logging.info("Секрет ITMO_REFRESH_TOKEN успешно обновлен в GitHub Secrets!")
        else:
            logging.warning(
                f"Не удалось обновить ITMO_REFRESH_TOKEN через gh CLI: {result.stderr.strip()}"
            )
    except Exception as err:
        logging.warning(f"Ошибка при вызове gh CLI: {err}")


def get_fresh_access_token() -> str | None:
    """Получает Access Token через Refresh Token или прямо из ITMO_ACCESS_TOKEN."""
    direct_token = os.getenv("ITMO_ACCESS_TOKEN")
    if direct_token:
        logging.info("Используется прямой ITMO_ACCESS_TOKEN из окружения.")
        return direct_token.strip()

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
        response = requests.post(TOKEN_URL, data=payload, timeout=15)
        if response.status_code == 400:
            logging.error(
                "Keycloak вернул 400 Bad Request: refresh-токен недействителен или протух! "
                "Требуется заново скопировать ITMO_REFRESH_TOKEN из браузера."
            )
            return None
        response.raise_for_status()
        data = response.json()

        new_access_token = data.get("access_token")
        new_refresh_token = data.get("refresh_token")

        if new_refresh_token:
            _save_new_refresh_token(new_refresh_token)

        return new_access_token

    except Exception as e:
        logging.error(f"Не удалось обновить токен через Keycloak: {e}")
        return None


def is_lesson_canceled(lesson: dict) -> bool:
    """Проверяет, отменено ли занятие по флагам в ответе API."""
    if lesson.get("is_canceled") is True or lesson.get("cancel") is True:
        return True
    status = str(lesson.get("status", "")).strip().lower()
    if status in ("canceled", "cancelled", "отменено", "отменена"):
        return True
    return False


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

    def sync_lessons(
        self,
        lessons_data: list,
        range_start_dt: str | None = None,
        range_end_dt: str | None = None,
    ) -> bool:
        """
        Синхронизирует расписание с базой данных SQLite:
        - Добавляет новые и обновляет изменившиеся пары.
        - Наглухо вырезает из базы отменённые пары и те, которые исчезли из API
          в пределах синхронизируемого диапазона дат [range_start_dt, range_end_dt].
        """
        has_changes = False
        active_pair_ids = set()

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            for lesson in lessons_data:
                # Если занятие помечено как отменённое — пропускаем (оно будет вычищено)
                if is_lesson_canceled(lesson):
                    continue

                pair_id = lesson.get("pair_id") or lesson.get("id") or lesson.get("lesson_id")
                if not pair_id:
                    continue

                date_str = lesson.get("date")
                start_time = lesson.get("time_start") or lesson.get("start_time")
                end_time = lesson.get("time_end") or lesson.get("end_time")

                if not (date_str and start_time and end_time):
                    continue

                start_dt = f"{date_str} {start_time}"
                end_dt = f"{date_str} {end_time}"

                subject = (
                    lesson.get("subject")
                    or lesson.get("title")
                    or lesson.get("subject_name")
                    or "Занятие"
                )
                work_type = lesson.get("work_type") or lesson.get("type") or lesson.get("type_name") or ""

                building = lesson.get("building") or ""
                room = lesson.get("room") or ""
                if building or room:
                    location = f"{building}, ауд. {room}".strip(", ")
                else:
                    location = (
                        lesson.get("format")
                        or lesson.get("lesson_format")
                        or "Дистанционно"
                    )

                teachers_list = lesson.get("teachers")
                if isinstance(teachers_list, list) and teachers_list:
                    teacher = ", ".join(
                        t.get("full_name") or t.get("name") or str(t)
                        for t in teachers_list
                        if isinstance(t, dict)
                    )
                else:
                    teacher = (
                        lesson.get("teacher_name")
                        or lesson.get("teacher")
                        or ""
                    )

                zoom_url = lesson.get("zoom_url") or lesson.get("link") or lesson.get("url") or ""

                active_pair_ids.add(pair_id)

                cursor.execute(
                    "SELECT subject, work_type, start_dt, end_dt, location, teacher, zoom_url FROM lessons WHERE pair_id = ?",
                    (pair_id,),
                )
                row = cursor.fetchone()
                new_data = (subject, work_type, start_dt, end_dt, location, teacher, zoom_url)

                if row is None:
                    cursor.execute(
                        """
                        INSERT INTO lessons (pair_id, subject, work_type, start_dt, end_dt, location, teacher, zoom_url)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (pair_id, *new_data),
                    )
                    has_changes = True
                elif row != new_data:
                    cursor.execute(
                        """
                        UPDATE lessons 
                        SET subject = ?, work_type = ?, start_dt = ?, end_dt = ?, location = ?, teacher = ?, zoom_url = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE pair_id = ?
                    """,
                        (*new_data, pair_id),
                    )
                    has_changes = True

            # Вырезаем отменённые и исчезнувшие из API пары в синхронизированном окне дат
            if range_start_dt and range_end_dt:
                if active_pair_ids:
                    placeholders = ",".join("?" for _ in active_pair_ids)
                    cursor.execute(
                        f"""
                        DELETE FROM lessons
                        WHERE start_dt >= ? AND start_dt <= ?
                          AND pair_id NOT IN ({placeholders})
                        """,
                        [range_start_dt, range_end_dt, *active_pair_ids],
                    )
                else:
                    # Если за этот период в API вообще нет активных пар
                    cursor.execute(
                        """
                        DELETE FROM lessons
                        WHERE start_dt >= ? AND start_dt <= ?
                        """,
                        [range_start_dt, range_end_dt],
                    )

                deleted_count = cursor.rowcount
                if deleted_count > 0:
                    logging.info(
                        f"Вырезано {deleted_count} отмененных/перенесенных пар в диапазоне {range_start_dt} - {range_end_dt}."
                    )
                    has_changes = True

            conn.commit()

        return has_changes

    def generate_ics(self, output_filename: str):
        """Формирует и сохраняет актуальный .ics файл со всеми активными парами."""
        cal = Calendar()
        cal.add("prodid", "-//ITMO Schedule Auto-Sync//ru//")
        cal.add("version", "2.0")
        cal.add("X-WR-CALNAME", "Расписание ИТМО")

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT pair_id, subject, work_type, start_dt, end_dt, location, teacher, zoom_url
                FROM lessons
                ORDER BY start_dt ASC
                """
            )
            rows = cursor.fetchall()

            # Тайминги для уведомлений: за 1 день, за 2 часа, за 1 час, за 15 минут
            alarms_config = [
                (timedelta(days=-1), "Завтра пара: "),
                (timedelta(hours=-2), "Через 2 часа: "),
                (timedelta(hours=-1), "Через 1 час: "),
                (timedelta(minutes=-15), "Через 15 минут: "),
            ]

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
                summary = f"{subject} ({work_type})" if work_type else subject
                event.add("summary", summary)
                event.add("dtstart", dt_start)
                event.add("dtend", dt_end)
                if location:
                    event.add("location", location)

                desc = []
                if teacher:
                    desc.append(f"Преподаватель: {teacher}")
                if zoom_url:
                    desc.append(f"Ссылка: {zoom_url}")
                if desc:
                    event.add("description", "\n".join(desc))

                event.add("uid", f"pair-{pair_id}@itmo.ru")

                for trigger_time, prefix in alarms_config:
                    alarm = Alarm()
                    alarm.add("action", "DISPLAY")
                    alarm.add("description", f"{prefix}{subject}")
                    alarm.add("trigger", trigger_time)
                    event.add_component(alarm)

                cal.add_component(event)

        with open(output_filename, "wb") as f:
            f.write(cal.to_ical())
        logging.info(f"Файл {output_filename} сгенерирован (всего пар: {len(rows)}).")


def parse_json_payload(raw_json) -> list:
    """Преобразует JSON-ответ расписания в плоский список занятий с датой."""
    flat_lessons = []
    data_content = (
        raw_json.get("data") if isinstance(raw_json, dict) else raw_json
    )

    if isinstance(data_content, list):
        for day in data_content:
            date_str = day.get("date")
            for lesson in day.get("lessons", []):
                lesson["date"] = date_str
                flat_lessons.append(lesson)
    elif isinstance(data_content, dict):
        for date_str, lessons in data_content.items():
            if isinstance(lessons, list):
                for lesson in lessons:
                    lesson["date"] = date_str
                    flat_lessons.append(lesson)

    return flat_lessons


def fetch_data_from_api(start_date: datetime, end_date: datetime) -> list:
    """Запрашивает расписание из API ИТМО в диапазоне дат [start_date, end_date]."""
    access_token = get_fresh_access_token()
    if not access_token:
        if os.path.exists("json_data.json"):
            logging.info("Используем локальный json_data.json для тестирования...")
            with open("json_data.json", "r", encoding="utf-8") as f:
                return parse_json_payload(json.load(f))
        return []

    headers = {
        **HEADERS,
        "Authorization": f"Bearer {access_token}",
    }

    all_lessons = []
    chunk_days = 14
    curr_start = start_date

    while curr_start < end_date:
        curr_end = min(curr_start + timedelta(days=chunk_days - 1), end_date)
        s_str = curr_start.strftime("%Y-%m-%d")
        e_str = curr_end.strftime("%Y-%m-%d")

        success = False
        for base_url in API_URLS:
            url = f"{base_url}?date_start={s_str}&date_end={e_str}"
            try:
                response = requests.get(url, headers=headers, timeout=12)
                if response.status_code == 200:
                    data = response.json()
                    parsed = parse_json_payload(data)
                    all_lessons.extend(parsed)
                    logging.info(
                        f"Получено {len(parsed)} занятий на период {s_str} — {e_str}"
                    )
                    success = True
                    break
                else:
                    logging.warning(
                        f"Эндпоинт {url} вернул HTTP {response.status_code}"
                    )
            except Exception as e:
                logging.error(f"Ошибка запроса {url}: {e}")

        if not success:
            logging.warning(f"Не удалось получить расписание на период {s_str} — {e_str}")

        curr_start = curr_end + timedelta(days=1)

    return all_lessons


def main():
    parser = argparse.ArgumentParser(
        description="ITMO Schedule Auto-Sync to ICS"
    )
    parser.add_argument(
        "--days-ahead",
        type=int,
        default=int(os.getenv("SYNC_DAYS_AHEAD", "14")),
        help="Количество дней вперед для синхронизации (по умолчанию: 14)",
    )
    parser.add_argument(
        "--days-behind",
        type=int,
        default=int(os.getenv("SYNC_DAYS_BEHIND", "1")),
        help="Количество дней назад для синхронизации (по умолчанию: 1)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        default=os.getenv("FULL_SYNC", "").lower() in ("true", "1", "yes"),
        help="Полная синхронизация на весь семестр (90 дней вперед)",
    )
    args = parser.parse_args()

    today = datetime.now().date()
    days_ahead = 90 if args.full else args.days_ahead
    days_behind = 7 if args.full else args.days_behind

    start_date = today - timedelta(days=days_behind)
    end_date = today + timedelta(days=days_ahead)

    range_start_dt = f"{start_date.strftime('%Y-%m-%d')} 00:00"
    range_end_dt = f"{end_date.strftime('%Y-%m-%d')} 23:59"

    logging.info(
        f"Запуск синхронизации на диапазон: {range_start_dt} — {range_end_dt} (full={args.full})"
    )

    manager = ScheduleManager(DB_FILE)
    lessons = fetch_data_from_api(start_date, end_date)

    if not lessons and not os.path.exists("json_data.json"):
        logging.warning("Нет свежих данных для синхронизации.")
        return

    # Синхронизируем базу с удалением отменённых/исчезнувших пар
    has_changes = manager.sync_lessons(
        lessons, range_start_dt=range_start_dt, range_end_dt=range_end_dt
    )

    # Пересобираем и обновляем .ics файл
    manager.generate_ics(ICS_FILE)
    logging.info("Синхронизация успешно завершена.")


if __name__ == "__main__":
    main()
