# Investment Assistant

Telegram-бот — инвестиционный ассистент поверх брокера Т-Инвестиции:

* **журнал гипотез**: `/idea` записывает решение buy/sell/hold с текущей ценой, `/review` проверяет,
  подтверждается ли гипотеза, `/close` фиксирует результат;
* **рыночные данные и портфель** через MCP-сервер T-Invest: `/price`, `/stats` (SMA, волатильность,
  просадка, тренд), `/portfolio`;
* **свободный диалог** с любой LLM, у которой есть OpenAI-совместимый API (OpenAI, DeepSeek,
  OpenRouter, Ollama, LM Studio...). Модель сама вызывает MCP-инструменты, но только для чтения.

Ассистент **не торгует**: MCP-сервер запускается с `TINVEST_ALLOW_TRADING=0`, а LLM получает только
список разрешённых инструментов чтения.

Python 3.12 или новее.

## Архитектура

```
Telegram ──► bot.py (aiogram 3) ──► journal.py ──► storage.py (SQLite)
                 │                      │
                 └──► llm.py (агент) ───┴──► market.py ──► mcp_client.py ══stdio══► tinvest-mcp ──► T-Invest API
```

MCP-сервер по умолчанию — [t-invest-tinkoff-mcp](https://github.com/viptdv333-boop/t-invest-tinkoff-mcp).
Его инструменты `get_last_price`/`get_candles` работают с **фьючерсами MOEX** (например `SiZ6`, `BRZ6`),
портфель возвращается целиком. Чтобы подключить другой MCP-сервер, задайте его команду и имена
инструментов в `.env` (`TINVEST_MCP_COMMAND`, `TINVEST_MCP_PRICE_TOOL` и т. д.).

## Установка

~~~sh
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
python -m pip install -e ".[tinvest]"
copy .env.example .env            # заполните TELEGRAM_BOT_TOKEN, TINKOFF_TOKEN, при желании LLM_*
~~~

Токен T-Invest лучше выпустить «только для чтения»: https://www.tbank.ru/invest/settings/api/.
Для проверки без риска используйте песочницу (`TINVEST_SANDBOX=1` и токен песочницы).

## Запуск

~~~sh
investment-assistant              # или: python -m investment_journal
~~~

## Тесты

~~~sh
python -m unittest discover -s tests -v
~~~

Сетевые сервисы в тестах не нужны: MCP подменяется заглушкой или тестовым stdio-сервером
`tests/fake_tinvest_server.py`, HTTP к LLM — `httpx.MockTransport`, Telegram — подменённым `Bot`.

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
