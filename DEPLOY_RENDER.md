# PartSnap — публикация на Render

1. Создай новый GitHub-репозиторий и загрузи туда содержимое этой папки.
2. В Render создай **Web Service** из этого репозитория.
3. Render увидит `Dockerfile`; дополнительных build-команд не нужно.
4. После первого запуска PartSnap будет работать в demo-режиме.
5. В Environment добавь:
   - `OPENAI_API_KEY`
   - `OPENAI_MODEL`
   - `EBAY_CLIENT_ID`
   - `EBAY_CLIENT_SECRET`
6. Перезапусти сервис.

Важно: ключи никогда не вставляются в HTML/JavaScript и не коммитятся в GitHub.

После подключения eBay backend автоматически добавляет живые eBay-результаты к demo-выдаче.
Следующая интеграция — реальный каталог TecDoc/Web Service после получения доступа.
