# PartSnap MVP v0.10.2

Найден реальный баг v0.10/v0.10.1:
AI prompt фактически не просил `part_class` и `search_query_en`, поэтому frontend часто отправлял
русское название детали и `part_class=other`.

Исправлено:
- AI теперь обязан вернуть part_class + marketplace-friendly search query;
- backend НЕ полагается только на AI: сам определяет battery/wiper/cv_joint/etc. по тексту;
- если запрос слишком русский/общий, backend строит свой:
  `BOSCH S4 Autobatterie Starterbatterie`;
- battery автоматически попадает в eBay.de category 179846;
- при нулевой выдаче показываются реальные диагностические данные:
  какой запрос ушёл в eBay, какой класс определён, сколько eBay вернул до и после фильтра.

Это версия специально для того, чтобы перестать гадать и увидеть точную причину нулевой выдачи.
