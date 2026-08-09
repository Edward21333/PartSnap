# PartSnap MVP v0.12.4

Главное:
- eBay errors теперь пишутся в Render Logs с mode/status/query/category/response;
- ошибка одного eBay-запроса больше не ломает весь каскад;
- PartSnap пробует несколько вариантов запроса.

Для CV joint:
1. AI/search query;
2. OEM/маркировка как есть;
3. OEM без пробелов;
4. `outer CV joint`;
5. `Rzeppa outer CV joint` / `VW Touareg outer CV joint`.

Если один вариант получает 400/500 от eBay, следующий всё равно выполняется.
В интерфейсе попытки теперь показывают и сам query, а не только mode/count.

Цель: перестать получать просто `eBay API error` и одновременно сделать поиск устойчивее.
