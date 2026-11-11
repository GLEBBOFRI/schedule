# 📅 ITMO Schedule Auto-Sync to ICS

Автоматический сервис для синхронизации персонального расписания ИТМО (my.itmo.ru), хранения в SQLite и выгрузки в `.ics` календарь (Apple Calendar, Google Calendar, Yandex Calendar).

---

## 🚀 Возможности
- **Скользящее окно синхронизации**: по умолчанию обновляет расписание на **2 недели вперёд** (и 1 день назад).
- **Наглухо вырезает отменённые пары**: если пару отменили в ИТМО, или перенесли (изменился ID/время), старая пара немедленно удаляется из базы и исчезает из календаря.
- **Уведомления**: 4 аларма перед каждой парой (за 1 день, за 2 часа, за 1 час, за 15 минут).
- **GitHub Actions + Pages**: автоматический запуск каждые 6 часов и раздача календаря по постоянной ссылке подписки.
- **Безопасная ротация Refresh Token**: поддержка `GH_PAT` для автоматического обновления секрета в репозитории при смене токена Keycloak.

---

## 🔑 1. Как получить `ITMO_REFRESH_TOKEN`

Для постоянной работы сервиса нужен именно **Refresh Token** (долгоживущий), а не кратковременный Access Token:

1. Открой браузер и перейди на [my.itmo.ru](https://my.itmo.ru).
2. Нажми **F12** (или `Cmd + Option + I` на macOS), открой вкладку **Сеть (Network)**.
3. В фильтре введи `token`.
4. Перезагрузи страницу (**Cmd + R** / **F5**).
5. Найди запрос к `https://id.itmo.ru/.../openid-connect/token`.
6. Перейди во вкладку **Ответ (Response)**:
   ```json
   {
     "access_token": "...",
     "refresh_token": "eyJhbGciOi...",
     ...
   }
   ```
7. Скопируй значение поля `refresh_token` (начинается с `ey...`).
8. Вставь его в репозиторий: **Settings** $\rightarrow$ **Secrets and variables** $\rightarrow$ **Actions** $\rightarrow$ секрет `ITMO_REFRESH_TOKEN`.

> **💡 Альтернативный способ:** В DevTools $\rightarrow$ вкладка **Application / Хранилище** $\rightarrow$ **Cookies** $\rightarrow$ `https://my.itmo.ru` $\rightarrow$ найди куку `auth._refresh_token.itmoId` (или `auth._token.itmoId`).

---

## 🤖 2. Настройка вечной работы (GH_PAT)

Keycloak в ИТМО регулярно ротирует refresh-токены. Чтобы бот сам обновлял секрет `ITMO_REFRESH_TOKEN` в репозитории:

1. Создай токен на GitHub: [github.com/settings/tokens](https://github.com/settings/tokens).
2. Выбери **Tokens (classic)** $\rightarrow$ **Generate new token**.
3. Отметь галочку `repo` (полный доступ к репозиториям).
4. Скопируй созданный токен (начинается с `ghp_...`).
5. В репозитории добавь секрет: **Settings** $\rightarrow$ **Secrets and variables** $\rightarrow$ **Actions** $\rightarrow$ **New repository secret**:
   - Имя: `GH_PAT`
   - Значение: твой `ghp_...`

Теперь бот при каждой ротации токена будет сам перезаписывать `ITMO_REFRESH_TOKEN` через GitHub API, и синхронизация никогда не умрёт!

---

## 💻 3. Локальный запуск

```bash
# Установка зависимостей
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Тестирование логики
python test_sync.py

# Обычный запуск (на 14 дней вперёд)
export ITMO_REFRESH_TOKEN="твой_токен"
python update_schedule.py

# Полная синхронизация на весь семестр (90 дней)
python update_schedule.py --full
```

---

## 📲 4. Подключение к Apple / Google Calendar

Твой календарь публикуется через GitHub Pages по ссылке:
```text
https://username.github.io/repo/itmo_schedule.ics
```
Для Apple Calendar добавь как подписку (webcal):
- Открой **Календарь** $\rightarrow$ **Файл** $\rightarrow$ **Новая подписка на календарь...**
- Вставь ссылку и поставь автообновление «Каждый час» или «Каждый день».
