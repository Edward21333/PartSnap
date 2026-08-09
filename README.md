# PartSnap MVP v0.13

Главное изменение: PartSnap теперь оценивает не только релевантность текста, но и тип найденной детали.

Каждое eBay-предложение получает одну из категорий:
- Точное совпадение по типу
- Вероятное совпадение
- Похожая деталь
- Не та деталь

Для CV joint:
- Gelenksatz / Außengelenk / CV joint -> exact/likely
- Antriebswelle / driveshaft -> similar
- boot-only / wheel bearing / sensor -> reject

Нерелевантные предложения удаляются из выдачи.
Сортировка:
semantic class -> EU -> spec fit -> eBay fitment -> relevance -> landed price.

Верхний KPI теперь называется:
`лучшая цена подходящей детали`
и берётся только из exact/likely результатов.
