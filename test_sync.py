import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from update_schedule import ScheduleManager, is_lesson_canceled, parse_json_payload

class TestScheduleSync(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_schedule.db")
        self.ics_path = os.path.join(self.tmp_dir.name, "test_schedule.ics")
        self.manager = ScheduleManager(self.db_path)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_cancellation_detection(self):
        self.assertTrue(is_lesson_canceled({"is_canceled": True}))
        self.assertTrue(is_lesson_canceled({"cancel": True}))
        self.assertTrue(is_lesson_canceled({"status": "canceled"}))
        self.assertTrue(is_lesson_canceled({"status": "отменено"}))
        self.assertFalse(is_lesson_canceled({"status": "active"}))
        self.assertFalse(is_lesson_canceled({}))

    def test_sync_and_wipe_cancelled_lessons(self):
        # 1. Сначала добавляем две пары на 2026-11-15
        initial_lessons = [
            {
                "pair_id": 101,
                "subject": "Мат Анализ",
                "work_type": "Лекции",
                "date": "2026-11-15",
                "time_start": "10:00",
                "time_end": "11:30",
                "building": "Кронверкский пр.",
                "room": "101",
                "teacher_name": "Иванов И.И."
            },
            {
                "pair_id": 102,
                "subject": "Физика",
                "work_type": "Практика",
                "date": "2026-11-15",
                "time_start": "11:40",
                "time_end": "13:10",
                "building": "Ломоносова ул.",
                "room": "202",
                "teacher_name": "Петров П.П."
            }
        ]
        range_start = "2026-11-10 00:00"
        range_end = "2026-11-20 23:59"

        self.manager.sync_lessons(initial_lessons, range_start, range_end)

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT pair_id FROM lessons")
            ids = [r[0] for r in cursor.fetchall()]
            self.assertEqual(sorted(ids), [101, 102])

        # 2. Теперь моделируем отмену:
        # Физику (102) отменили в API (исчезла из выдачи), а остался только Мат Анализ (101)
        updated_lessons = [
            {
                "pair_id": 101,
                "subject": "Мат Анализ",
                "work_type": "Лекции",
                "date": "2026-11-15",
                "time_start": "10:00",
                "time_end": "11:30",
                "building": "Кронверкский пр.",
                "room": "101",
                "teacher_name": "Иванов И.И."
            }
        ]
        self.manager.sync_lessons(updated_lessons, range_start, range_end)

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT pair_id FROM lessons")
            ids = [r[0] for r in cursor.fetchall()]
            # Пара 102 должна быть НАГЛУХО ВЫРЕЗАНА!
            self.assertEqual(ids, [101])

        # 3. Моделируем пару с флагом is_canceled=True в API:
        canceled_payload = [
            {
                "pair_id": 101,
                "subject": "Мат Анализ",
                "work_type": "Лекции",
                "date": "2026-11-15",
                "time_start": "10:00",
                "time_end": "11:30",
                "is_canceled": True
            }
        ]
        self.manager.sync_lessons(canceled_payload, range_start, range_end)

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT count(*) FROM lessons")
            count = cursor.fetchone()[0]
            # Все отмененные пары вычищены!
            self.assertEqual(count, 0)

    def test_ics_generation(self):
        lessons = [
            {
                "pair_id": 201,
                "subject": "Октавиеведение (MQB)",
                "work_type": "Лабораторная",
                "date": "2026-11-16",
                "time_start": "09:00",
                "time_end": "10:30",
                "building": "Гараж",
                "room": "Подъёмник 1",
                "teacher_name": "Главный Инженер",
                "zoom_url": "https://octavia.vag"
            }
        ]
        self.manager.sync_lessons(lessons, "2026-11-15 00:00", "2026-11-20 23:59")
        self.manager.generate_ics(self.ics_path)

        self.assertTrue(os.path.exists(self.ics_path))
        with open(self.ics_path, "r", encoding="utf-8") as f:
            content = f.read()
            self.assertIn("Октавиеведение (MQB)", content)
            self.assertIn("pair-201@itmo.ru", content)
            self.assertIn("Подъёмник 1", content)
            self.assertIn("Завтра пара: Октавиеведение (MQB)", content)

if __name__ == "__main__":
    unittest.main()
