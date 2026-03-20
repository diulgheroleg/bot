ReBootFix Booking Bot (updated)
===============================

Что умеет:
- /start с параметрами deep-link (модель+услуга) или консультация
- сбор проблемы -> (для экрана) подбор вариантов дисплея из parts_prices.json (из Excel)
- показывает стоимость (диапазон или точная цена варианта)
- спрашивает телефон (автопрефикс +7 9) -> дата (14 дней) -> время
- отправляет заявку админу (ADMIN_USER_ID) красиво
- "Связаться с мастером": сообщения клиента пересылаются админу, админ отвечает reply -> клиент получает ответ

Запуск:
1) Скопируй .env.example в .env и заполни BOT_TOKEN и ADMIN_USER_ID
2) pip install -r requirements.txt
3) python bot.py

Deep-links:
- Запись с сайта: https://t.me/<BOT_USERNAME>?start=b|iphone|iPhone%2017|screen
- Консультация:    https://t.me/<BOT_USERNAME>?start=consult
