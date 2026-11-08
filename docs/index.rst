Investment Assistant
====================

Telegram-бот — инвестиционный ассистент: журнал гипотез buy/sell/hold, рыночные
данные и портфель из Т-Инвестиций через MCP-сервер, свободный диалог с LLM.

Модели
------

.. automodule:: investment_journal.models
   :members:
   :member-order: bysource

Настройки
---------

.. automodule:: investment_journal.config
   :members: McpSettings, LlmSettings, Settings, load_settings, read_dotenv
   :member-order: bysource

MCP-клиент и рыночные данные
----------------------------

.. automodule:: investment_journal.mcp_client
   :members: ToolError, McpToolClient, parse_tool_result
   :member-order: bysource

.. automodule:: investment_journal.market
   :members: MarketDataService
   :member-order: bysource

Журнал и аналитика
------------------

.. automodule:: investment_journal.journal
   :members:
   :member-order: bysource

.. automodule:: investment_journal.analytics
   :members: DecisionReview, CandleStats, review_decision, candle_stats
   :member-order: bysource

LLM-агент
---------

.. automodule:: investment_journal.llm
   :members: ChatCompletionsClient, InvestmentAgent
   :member-order: bysource
