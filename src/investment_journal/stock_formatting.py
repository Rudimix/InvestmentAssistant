"""Карточки подбора с исходными периодами и явными пропусками оценок."""

from collections import Counter
from decimal import Decimal
from html import escape

from .portfolio_formatting import plain_chunks


MODES = {"fundamental": "Фундаментальный", "technical": "Технический", "combined": "Совместный"}
METRICS = {"pe": "P/E", "pb": "P/B", "ev_ebitda": "EV/EBITDA", "net_debt_to_ebitda": "Чистый долг / EBITDA",
           "revenue_growth": "Изменение выручки", "net_profit_growth": "Изменение прибыли"}
FEATURES = {"momentum_1m": "Моментум за месяц", "momentum_3m": "Моментум за 3 месяца", "momentum_12m": "Моментум за 12 месяцев",
            "sma50": "SMA 50", "sma200": "SMA 200", "sma_spread": "SMA 50 относительно SMA 200", "rsi14": "RSI 14",
            "volatility": "Дневная волатильность", "drawdown": "Просадка по истории", "relative_volume": "Относительный объём"}


def number(value):
    return "недоступно" if value is None else f"{Decimal(str(value)):.2f}"


def candidate_lines(candidate, mode):
    entry = candidate["entry"]
    lines = [f"{entry['ticker']} · {entry['class_code']} · сектор {entry['sector'] or 'не подтверждён'}",
             f"Исходная цена {number(entry['price'])} {entry['currency'].upper()} · дата свечи {str(entry['price_at'])[:10]}",
             f"Отраслевой ранг {number(candidate['score'])} из 100"]
    if mode != "technical":
        fundamental = candidate["fundamental"]
        for name, metric in fundamental["metrics"].items():
            suffix = "%" if metric["unit"] == "percent" else ""
            lines.append(f"{METRICS[name]} — {number(metric['value'])}{suffix} · {metric['period_start']}–{metric['period_end']} · {metric['standard']} · {metric['currency'].upper()}")
            if metric.get("capitalization_scope") == "catalog_classes":
                lines.append("Основа мультипликатора · суммарная капитализация известных классов в проверенной выборке")
            peer = fundamental["comparisons"].get(name)
            if peer:
                lines.append(f"Медиана сопоставимых эмитентов {number(peer['median'])} · отклонение ниже медианы {number(peer['discount_percent'])}%")
                lines.append("Сопоставимые бумаги · "+", ".join(peer["peer_ids"]))
                if peer["potential_undervaluation"]:
                    lines.append("Мультипликатор ниже отраслевой медианы, есть признак потенциальной недооценённости")
            lines.append("Финансовые факты · "+", ".join(metric["fact_ids"]))
        lines.extend(fundamental["reasons"])
        for evidence in candidate.get("evidence", []):
            fact = evidence["fact"]
            lines.append(f"Источник {evidence['source']} · {fact['id']} · страница {evidence['page'] or 'не указана'} · {evidence['url']}")
            lines.append(f"Версия {evidence['document_version']} · доступно с {fact['available_at']}")
    if mode != "fundamental":
        technical = candidate["technical"]
        for name, value in technical["features"].items():
            unit = entry["currency"].upper() if name in {"sma50", "sma200"} else "" if name in {"rsi14", "relative_volume"} else "%"
            lines.append(f"{FEATURES[name]} — {number(value)} {unit}".rstrip())
        lines.extend(technical["signals"]+technical["reasons"])
        lines.append("Версия дневной истории · "+technical["series_version"])
    if mode == "combined":
        lines.append({"agree": "Отраслевые сигналы двух методов согласуются", "conflict": "Отраслевые сигналы двух методов противоречат друг другу",
                      "unavailable": "Для совместного ранга недостаточно обязательных данных"}[candidate["combined"]["agreement"]])
    suitability = candidate["suitability"]
    lines.append({"conditional": "Личные лимиты для одного лота предварительно соблюдены", "restricted": "Есть конфликт с личными ограничениями или доступностью",
                  "unknown": "Личная пригодность требует дополнительных данных"}[suitability["status"]])
    lines.extend(suitability["reasons"])
    for name, value in suitability["metrics"].items():
        label = {"one_lot_before_costs": "Один лот до комиссии", "cash_after_before_costs": "Остаток после одного лота до комиссии",
                 "position_after_pct": "Доля бумаги после одного лота", "sector_after_pct": "Доля сектора после одного лота"}[name]
        unit = "%" if name.endswith("pct") else entry["currency"].upper()
        lines.append(f"{label} — {number(value)} {unit}")
    forecast = candidate.get("forecast", {})
    if forecast.get("available"):
        record, card = forecast["forecast"], forecast["model_card"]
        lines.append(f"Прогноз на 12 месяцев · до {record['target_at']} · {record['currency'].upper()}")
        source = Decimal(record["source_price"])
        for name, label in (("low_price", "Неблагоприятный сценарий"), ("base_price", "Базовый сценарий"), ("high_price", "Благоприятный сценарий")):
            value = Decimal(record[name])
            lines.append(f"{label} — {number(value)} {record['currency'].upper()} · изменение {number((value/source-1)*100)}%")
        lines.extend(record["limitations"])
        lines.append(f"Версия модели {record['model_version']} · {card['feature_set']} · обучение до {card['trained_until']}")
        validation = card["validation"]
        lines.append(f"Временная проверка базовой Ridge до коррекции смещения · {validation['count']} прогнозов · MAE логарифмической доходности {number(validation['mae_log'])}")
        lines.append(f"Сохранение цены · MAE {number(validation['persistence_mae_log'])} · смещение модели {number(validation['bias_log'])}")
    else:
        lines.append("Прогноз на 12 месяцев · "+forecast.get("reason", "проверенная модель недоступна"))
    return lines


def screening_parts(screening, universe):
    lines = [f"Подбор акций · {MODES[screening['mode']]} режим", f"Дата оценки {screening['as_of']}",
             f"Критерий бизнеса · countryOfRisk RUS", f"Дата состава выборки {universe.as_of.isoformat()}",
             f"В каталоге {universe.catalog_count} записей · проверено {len(universe.entries)} бумаг · прошло {len(screening['candidates'])}",
             f"Профиль · ревизия {screening['profile_revision'] or 'отсутствует'}", f"Методика {screening['methodology']}",
             "Ранги сравнивают бумаги одной отрасли с равными весами двух блоков в совместном режиме.",
             "Исторический состав каталога восстановлен частично."]
    policy = universe.inputs.get("policy", {})
    lines.append(f"Ликвидность · медиана за {policy.get('liquidity_days')} свечей от {policy.get('minimum_turnover_rub')} RUB в день, давность цены до {policy.get('maximum_price_age_days')} дней")
    for candidate in screening["candidates"][:10]:
        lines.append("")
        lines.extend(candidate_lines(candidate, screening["mode"]))
    if not screening["candidates"]:
        lines.append("Кандидаты по подтверждённым данным отсутствуют")
    excluded = Counter(reason for entry in universe.entries if not entry.included for reason in entry.reasons)
    if excluded:
        lines.append("Причины исключений из выборки")
        lines.extend(f"{reason} · {count} бумаг" for reason, count in excluded.most_common())
    lines.extend(screening["quality"])
    lines.extend([f"Подборка {screening['id']}", f"Карточка любой проверенной бумаги · /stocks бумага {screening['id']} ТИКЕР",
                  f"Гипотеза по кандидату · /stocks гипотеза {screening['id']} ТИКЕР",
                  f"Проверка прогноза · /stocks наблюдение {screening['id']} ТИКЕР",
                  f"Следующие кандидаты · /stocks кандидаты {screening['id']} 2",
                  "Сохранённые отчёты · /stocks история"])
    return [escape(chunk) for chunk in plain_chunks("\n".join(lines), 3500)]
