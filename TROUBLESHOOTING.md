# Ошибки OpenAI API

RespondService использует `OPENAI_API_KEY` для генерации текста (`gpt-6-luna`) и изображений карусели (`gpt-image-2.5-flare`). Добавьте действующий ключ в `.env` и перезапустите backend:

```env
OPENAI_API_KEY=ваш_ключ_openai
```

Если запрос завершается таймаутом, повторите его позже, сократите исходный текст и проверьте сетевой доступ к API из машины, где запущен backend. Доступность домена без ключа можно проверить в PowerShell:

```powershell
Test-NetConnection api.openai.com -Port 443
```

Не передавайте API-ключ в чат, логи или браузер.

## Генерация видео Reels

OpenAI Videos API остановлена и сейчас не предоставляет endpoint для создания MP4. RespondService по-прежнему генерирует сценарий и таймлайн Reels через OpenAI, но не формирует видеофайл. Endpoint `/api/v1/render-reels` возвращает HTTP 503; установка FFmpeg или добавление API-ключа это ограничение не устраняет.

## Когда обращаться к разработчику

Передайте время ошибки, выбранные форматы и размер исходного текста. API-ключ и полный текст ошибки с секретами передавать нельзя.

## Пользовательские социальные аккаунты

Социальные токены больше не читаются из `.env`. Backend хранит аккаунты в `respondservice.sqlite3`, а access token шифруется ключом `TOKEN_ENCRYPTION_KEY`.

Добавьте в `.env`:

```env
TOKEN_ENCRYPTION_KEY=<Fernet-ключ>
META_APP_ID=<Meta App ID>
META_APP_SECRET=<Meta App Secret>
META_OAUTH_REDIRECT_URI=https://your-domain.example/api/v1/oauth/meta/callback
FRONTEND_URL=https://your-frontend.example/
```

При необходимости для отдельных Meta-продуктов можно задать `INSTAGRAM_APP_ID` / `INSTAGRAM_APP_SECRET`, `FACEBOOK_APP_ID` / `FACEBOOK_APP_SECRET` и `THREADS_APP_ID` / `THREADS_APP_SECRET`. Иначе используется общий `META_APP_ID` / `META_APP_SECRET`.

OAuth-потоки разделены: Instagram использует Instagram Login и `instagram_business_basic,instagram_business_content_publish`, Facebook использует Facebook Login и получает Pages, Threads использует `threads.net/oauth/authorize` и `graph.threads.net/oauth/access_token`.

Сгенерировать ключ можно командой:

```powershell
.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

После входа frontend получает список аккаунтов через `/api/v1/social-accounts`. Для публикации он передаёт `Authorization: Bearer ...` и `X-Account-Id`; публикация без выбранного аккаунта отклоняется.