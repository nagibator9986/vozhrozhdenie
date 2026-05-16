# 🚀 Деплой на Railway

Пошаговый гайд для развёртывания Telegram + WhatsApp консультанта на Railway.

**Время:** ~30–40 минут от нуля до работающего бота в проде.
**Стоимость:** $5 стартового кредита + ~$5–10/мес на Railway после, плюс OpenAI/Anthropic API по факту использования.

---

## 📋 Что понадобится

- Аккаунт GitHub
- Аккаунт Railway (с привязанной картой — Railway не списывает до окончания $5 кредита, но карта обязательна для верификации)
- Доступ к BotFather в Telegram (для ротации токена)
- Доступ к кабинету OpenAI / Wazzup24 / Telegraph (для ротации ключей)
- `openssl` локально (для генерации webhook-секрета)

---

## Шаг 0. Подготовка локально

### 0.1. Ротация секретов

Текущие ключи в `.env` уже могли «засветиться» — перед прод-деплоем выпусти новые:

| Сервис | Где |
|--------|-----|
| Telegram bot token | @BotFather → `/mybots` → выбрать бота → `API Token` → `Revoke current token` |
| OpenAI API key | platform.openai.com → API Keys → Revoke old → `Create new secret key` |
| Wazzup API key | Кабинет Wazzup24 → Настройки → API → Перевыпустить |
| Telegraph token | `curl "https://api.telegra.ph/revokeAccessToken?access_token=СТАРЫЙ"` |

### 0.2. Сгенерировать webhook secret

```bash
openssl rand -hex 32
```

Скопируй результат — понадобится в шагах 1.4 и 3.

### 0.3. Почистить тестовый мусор

```bash
cd /Users/a1111/Desktop/projects/win/telegram-vozrozhdenie
rm -f data/bot_h30.db data/bot_harness_test.db data/bot_smoke_*.db data/h30_sc*.db data/test_20_live.db
rm -f bot.log
find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null
```

### 0.4. Инициализировать git и запушить на GitHub

```bash
git init
git add .
git status         # ← убедись что .env НЕ в списке (его исключает .gitignore)
git commit -m "Initial commit"
```

Создай **приватный** репозиторий на github.com (например `vozrozhdenie-bot`), затем:

```bash
git remote add origin git@github.com:USER/vozrozhdenie-bot.git
git branch -M main
git push -u origin main
```

---

## Шаг 1. Создать сервис на Railway

### 1.1. Регистрация

1. Открыть [railway.com](https://railway.com) → **Sign up with GitHub**
2. Привязать карту (`Account Settings` → `Billing` → `Add Payment Method`)

### 1.2. Новый проект из репозитория

`Dashboard` → **+ New Project** → **Deploy from GitHub repo** → выбрать `vozrozhdenie-bot` → **Deploy Now**

Railway автоматически найдёт `Dockerfile` и начнёт сборку.
Первый билд ~6–10 мин (скачивается sentence-transformer модель ~500MB).

### 1.3. Persistent volume для SQLite + ChromaDB

`Settings` → **Volumes** → **+ New Volume**:

- Mount path: **`/app/state`**  ← **именно сюда, НЕ на `/app/data`!**
- Size: `1 GB`

> ⚠️ **Почему не `/app/data`:** Docker volume перекрывает содержимое образа. Видео-файлы (476 MB) baked-in в `/app/media-videos` внутри образа, БД/ChromaDB/логи пишутся в `/app/state` (это и есть volume). Если смонтировать на `/app/data` — видео не сломаются (они теперь в `/app/media-videos`), но это бесполезный путь.

Без volume база и векторный индекс будут стираться при каждом редеплое.

### 1.4. Environment variables

`Settings` → **Variables** → **Raw Editor** → вставить (заменив значения на свои):

```env
TELEGRAM_BOT_TOKEN=новый_токен_от_BotFather
OPENAI_API_KEY=sk-новый_ключ_OpenAI
AI_PROVIDER=openai
MODEL_NAME=gpt-4o
MAX_RESPONSE_TOKENS=6000

WAZZUP_ENABLED=true
WAZZUP_API_KEY=новый_wazzup_ключ
WAZZUP_CHANNEL_ID=9a515b32-a33e-4c21-baf6-bab908170d1b
WAZZUP_WEBHOOK_PORT=8080
WAZZUP_WEBHOOK_PATH=/wazzup/webhook
WAZZUP_WEBHOOK_SECRET=твой_secret_из_шага_0.2

TELEGRAPH_ACCESS_TOKEN=новый_telegraph_token

KASPI_PRODUCT_URL=https://l.kaspi.kz/shop/GqxNzHCFTPkUewL
KASPI_CAPSULES_URL=https://l.kaspi.kz/shop/ECdTcYiZ2ghs9dX
WHATSAPP_URL=https://wa.me/77072886419
OFFICE_ADDRESS=MTI Medical, г. Алматы, ул. Абиш Кикелбайулы, д. 254, блок 5, 1 этаж, вход отдельный, зелёная вывеска MTI Group

VIDEO_NUZUM_URL=https://www.instagram.com/p/CzByWariWzd/?hl=ru
VIDEO_JOHN_GRAY_URL=https://www.instagram.com/p/DVkisQGDezc/?hl=ru
VIDEO_BROWNSTEIN_URL=https://www.instagram.com/p/CxFkyFosZ_b/?hl=ru
VIDEO_FLECHAS_URL=https://www.instagram.com/p/CxHc7_AIOei/?hl=ru
VIDEO_DOSAGE_URL=https://www.instagram.com/p/DV-_LLVjX9X/?hl=ru
YANDEX_DISK_REVIEWS_URL=https://disk.yandex.ru/d/Ft8d4EBf8ZGv8A
```

После Save Railway автоматически перезапустит сервис.

### 1.5. Публичный HTTPS-домен

`Settings` → **Networking** → **Generate Domain** → порт `8080`.

Получишь URL вида `vozrozhdenie-bot-production.up.railway.app`.

Добавь ещё две переменные (используй полученный домен):

```env
VIDEO_BASE_URL=https://vozrozhdenie-bot-production.up.railway.app/media/videos
ARTICLES_BASE_URL=https://vozrozhdenie-bot-production.up.railway.app/articles
```

---

## Шаг 2. Проверка запуска

`Deployments` → последний → **View Logs**. Должны появиться:

```
SQLite tuned: WAL + busy_timeout=5s
Database ready.
ArticlesService: loaded N articles.
Telegraph cache primed: N entries.
RAG knowledge base ready.
Starting dual mode: Telegram polling + Wazzup webhook on port 8080
Wazzup webhook server starting on port 8080
```

### Healthcheck

```bash
curl https://vozrozhdenie-bot-production.up.railway.app/health
# → {"status":"ok"}
```

### Telegram

Написать боту `/start` в Telegram — должен ответить приветствием воронки.

---

## Шаг 3. Подключить Wazzup webhook

Кабинет Wazzup24 → **Настройки** → **Интеграции** → **API и webhook**.

**Webhook URL** (обязательно с токеном в query — это твоя защита от спама):

```
https://vozrozhdenie-bot-production.up.railway.app/wazzup/webhook?token=ТВОЙ_WAZZUP_WEBHOOK_SECRET
```

**Подписки на события**: `messages`

Save → в логах Railway увидишь:

```
Wazzup webhook test received — OK
```

Если в логах `401 Unauthorized` — токен в URL не совпадает с переменной `WAZZUP_WEBHOOK_SECRET`. Исправь и сохрани заново.

---

## Шаг 4. Прод-чеклист

| Проверка | Команда / действие | Ожидаемо |
|----------|--------------------|----------|
| Healthcheck отвечает | `curl https://домен/health` | `{"status":"ok"}` |
| Webhook требует токен | `curl -X POST https://домен/wazzup/webhook -d '{}'` (без токена) | `401 Unauthorized` |
| Telegram отвечает | Написать `/start` боту | Текст приветствия |
| WhatsApp отвечает | Написать боту на `+7 707 288 64 19` | Текст приветствия |
| Volume примонтирован | После рестарта `/app/data/bot.db` сохраняется | БД на месте после redeploy |

---

## 💰 Стоимость

### Railway
- 1 vCPU, ~1 GB RAM, 24/7 → ~$5–10/мес
- Egress бесплатно до 100 GB/мес
- $5 стартовых кредитов на ~1–2 недели

### LLM API (отдельно — это твой счёт OpenAI/Anthropic)

| Модель | Один ответ | 100 ответов/день |
|--------|-----------|------------------|
| `gpt-4o` | ~$0.025 | ~$75/мес |
| `gpt-4o-mini` | ~$0.003 | ~$9/мес |
| `claude-haiku-4-5` | ~$0.005 | ~$15/мес |

Если хочется дешевле — поменять `MODEL_NAME=gpt-4o-mini` (тот же провайдер) или переключиться на Anthropic.

---

## 🔄 Обновления

```bash
# Локально внёс правку
git add <файлы>
git commit -m "fix: что починил"
git push origin main
```

Railway автоматически увидит push и задеплоит за ~3 мин.

---

## 🛟 Если что-то сломалось

| Симптом | Что делать |
|---------|-----------|
| Build падает | `Deployments` → клик на failed → читаем логи. Обычно — отсутствующая зависимость в `requirements.txt`. |
| Бот стартует, но не отвечает в Telegram | Проверь `TELEGRAM_BOT_TOKEN` и что у бота не отозвали токен. |
| Wazzup webhook не доходит | Логи покажут `401` — токен. `Bad JSON` — Wazzup шлёт что-то странное. Молчание — URL некорректный. |
| `database is locked` под нагрузкой | Это сигнал что пора переходить на Postgres (см. `requirements.txt` — раскомментировать `asyncpg` + поменять `DATABASE_URL`). |
| Бот «забыл» данные после redeploy | Volume не примонтирован. Перепроверь Шаг 1.3. |
| OpenAI ловит rate limit | Снизь `LLM_MAX_CONCURRENT` (по умолчанию 20) или подними тир в OpenAI. |
| Расходы на gpt-4o высокие | Переключи `MODEL_NAME=gpt-4o-mini` — в 8 раз дешевле, качество чуть хуже. |

---

## ✅ Финальный чеклист перед первым стартом

- [ ] Telegram bot token перевыпущен
- [ ] OpenAI key перевыпущен
- [ ] Wazzup API key перевыпущен
- [ ] Telegraph token перевыпущен
- [ ] `WAZZUP_WEBHOOK_SECRET` сгенерирован (`openssl rand -hex 32`)
- [ ] `.env` НЕ закоммичен (проверь `git ls-files | grep env`)
- [ ] Тестовые БД (`bot_h30.db`, `bot_smoke_*.db`, `h30_sc*.db`, `test_20_live.db`) удалены
- [ ] Volume `/app/data` создан и примонтирован
- [ ] Все env vars из Шага 1.4 заданы в Railway
- [ ] `VIDEO_BASE_URL` и `ARTICLES_BASE_URL` указывают на Railway-домен
- [ ] Wazzup webhook URL сконфигурирован с `?token=...`
- [ ] `/health` отвечает 200 OK
- [ ] Тестовое сообщение в Telegram прошло
- [ ] Тестовое сообщение в WhatsApp прошло
