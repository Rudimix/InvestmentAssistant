# Investment Assistant

Telegram-бот — инвестиционный ассистент поверх брокера Т-Инвестиции:

* **журнал гипотез**: `/idea` записывает решение buy/sell/hold с текущей ценой, `/review` проверяет,
  подтверждается ли гипотеза, `/close` фиксирует результат;
* **рыночные данные и портфель** через [официальный MCP-сервер Т-Инвестиций](https://developer.tbank.ru/invest/mcp):
  `/price`, `/stats` (SMA, волатильность, просадка, тренд), `/portfolio`. Работают акции, облигации,
  фонды и фьючерсы: тикер `SBER` сам переводится в `SBER_TQBR`, `SiZ6` — в `SiZ6_SPBFUT`;
* **свободный диалог** с любой LLM, у которой есть OpenAI-совместимый API (OpenAI, DeepSeek,
  OpenRouter, Ollama, LM Studio...). Модель сама вызывает MCP-инструменты, но только для чтения.

Ассистент **не торгует**: LLM получает только список инструментов чтения (без заявок, стоп-заявок
и операций со счетами), а для бота стоит выпускать токен «Только чтение» — тогда торговать
запретит сам сервер Т-Инвестиций.

Python 3.12 или новее.

## Архитектура

```
Telegram ──► bot.py (aiogram 3) ──► journal.py ──► storage.py (SQLite)
                 │                      │
                 └──► llm.py (агент) ───┴──► market.py ──► mcp_client.py ══HTTPS══► invest-public-api.tbank.ru/mcp
```

Сервер Т-Инвестиций использует сертификат Минцифры, которого нет в стандартном наборе Python.
Скачайте с https://www.gosuslugi.ru/crt корневой и выпускающий сертификаты и положите их в `certs/`
(пути задаются в `TINVEST_CA_FILES`). Они используются только для соединения бота с сервером
и в систему не устанавливаются.

## Установка

~~~sh
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
python -m pip install -e .
copy .env.example .env            # заполните TELEGRAM_BOT_TOKEN, TINKOFF_TOKEN, при желании LLM_*
~~~

Токен T-Invest: Т-Инвестиции → Настройки → Токены API, лучше «Только чтение».
Для проверки без риска используйте песочницу (`TINVEST_SANDBOX=1` и токен песочницы).

Проверить подключение и посмотреть инструменты сервера:

~~~sh
python -m investment_journal --list-tools
~~~

## Запуск

~~~sh
investment-assistant              # или: python -m investment_journal
~~~

## Тесты

~~~sh
python -m unittest discover -s tests -v
~~~

Сетевые сервисы в тестах не нужны: MCP подменяется заглушкой с форматом ответов официального
сервера или локальным тестовым HTTP-сервером `tests/fake_tinvest_server.py`, HTTP к LLM —
`httpx.MockTransport`, Telegram — подменённым `Bot`.

## Метрики кода (лабораторная работа №3)

~~~powershell
python -m pip install -e ".[metrics]"
.\scripts\metrics.ps1 -Out metrics\after
~~~

Скрипт запускает готовые инструменты и сохраняет их вывод в `metrics\after`:

* **radon** — индекс сопровождаемости, цикломатическая сложность, LOC/SLOC/LLOC, метрики Холстеда;
* **lizard** — CCN, NLOC и число параметров функций;
* **flake8 + mccabe** — правило C901 (порог в `.flake8`);
* **pylint** — глубина наследования и проверки дизайна (пороги в `pyproject.toml`);
* **pyscn** — CBO, LCOM4, связанность модулей, когнитивная сложность, дублирование (`pyscn.html`);
* **coverage.py** — покрытие тестами.

Отчёт по работе: [docs/lab3_report.md](docs/lab3_report.md).

## Документация

~~~sh
python -m pip install -e ".[docs]"
python -m sphinx -b html -n -W docs docs/_build/html
~~~

Откройте `docs/_build/html/index.html` в браузере.
