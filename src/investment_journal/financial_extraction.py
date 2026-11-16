"""Подтверждённые финансовые строки с единицами, периодами и страницей источника."""

from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import re

from pypdf import PdfReader

from .models import EvidenceChunk, EvidenceDocument, FinancialFact

MONTHS = {name: index for index, name in enumerate(("января", "февраля", "марта", "апреля", "мая", "июня",
                                                   "июля", "августа", "сентября", "октября", "ноября", "декабря"), 1)}
METRICS = {
    "выручка": "revenue", "чистая прибыль": "net_profit", "прибыль за год": "net_profit",
    "прибыль за период": "net_profit", "итого активов": "assets", "всего активы": "assets",
    "итого собственных средств": "equity", "итого капитал": "equity", "собственные средства": "equity",
    "чистые процентные доходы": "net_interest_income", "чистые комиссионные доходы": "net_fee_income",
    "средства клиентов": "customer_deposits", "кредиты и авансы клиентам": "loans",
    "чистые денежные средства от операционной деятельности": "operating_cash_flow",
    "ebitda": "ebitda", "скорректированная ebitda": "adjusted_ebitda", "общий долг": "total_debt",
    "денежные средства и их эквиваленты": "cash",
}
STOCK_METRICS = {"assets", "equity", "customer_deposits", "loans", "total_debt", "cash"}


def extract_pdf_pages(content: bytes) -> list[str]:
    """Сохранить столбцы PDF в текстовом представлении для проверки таблиц."""
    return [page.extract_text(extraction_mode="layout") or "" for page in PdfReader(BytesIO(content)).pages]


@dataclass
class ExtractedFinancials:
    chunks: list[EvidenceChunk] = field(default_factory=list)
    facts: list[FinancialFact] = field(default_factory=list)
    quality: list[str] = field(default_factory=list)


def _units(text: str):
    text = text.casefold()
    scales = []
    for pattern, scale in ((r"миллиард|млрд", 10**9), (r"миллион|млн", 10**6), (r"тысяч|тыс\.", 10**3)):
        if re.search(pattern, text):
            scales.append(Decimal(scale))
    currencies = [currency for pattern, currency in ((r"рубл|руб\.", "rub"), (r"доллар|\busd\b", "usd"), (r"евро|\beur\b", "eur"))
                  if re.search(pattern, text)]
    if len(scales) != 1 or len(currencies) != 1:
        return None
    return scales[0], currencies[0]


def _number(text: str) -> Decimal | None:
    text = text.strip().replace("\u00a0", " ").replace("\u202f", " ").replace("−", "-")
    if text in {"-", "—", "–", "н/д"}:
        return None
    if not re.fullmatch(r"\(?[+-]?\d[\d ]*(?:[,.]\d+)?\)?", text):
        raise ValueError("Непригодная числовая ячейка")
    if text.startswith("(") != text.endswith(")"):
        raise ValueError("Незавершённое отрицательное число")
    negative = text.startswith("(")
    value = Decimal(text.strip("()").replace(" ", "").replace(",", "."))
    return -value if negative else value


def _period(context: str, year: int, stock: bool):
    dates = re.findall(r"(\d{1,2})\s+("+"|".join(MONTHS)+r")\s+(20\d{2})", context.casefold())
    matches = {(int(day), MONTHS[month]) for day, month, candidate in dates if int(candidate) == year}
    if len(matches) > 1:
        return None
    common = {(int(day), MONTHS[month]) for day, month, _ in dates}
    if not matches:
        if stock or len(common) != 1:
            return None
        matches = common
    day, month = next(iter(matches))
    try:
        end = date(year, month, day)
    except ValueError:
        return None
    if stock:
        return end, end
    duration = re.search(r"за\s+(3|6|9|12|три|шесть|девять|двенадцать)\s+месяц", context.casefold())
    months = {"три": 3, "шесть": 6, "девять": 9, "двенадцать": 12}
    if duration:
        count = months.get(duration[1]) or int(duration[1])
    elif re.search(r"за\s+(?:\d{4}\s+)?год", context.casefold()):
        count = 12
    else:
        return None
    if day != monthrange(year, month)[1]:
        return None
    offset = year*12+month-count
    first_year, first_month = divmod(offset, 12)
    return date(first_year, first_month+1, 1), end


def extract_financials(document: EvidenceDocument, pages: list[str], instrument_id: str) -> ExtractedFinancials:
    """Поддержать явно размеченные рублёвые/валютные таблицы, сохраняя пробелы столбцов.

    Неоднозначные единицы, периоды, примечания и конфликтующие строки
    исключаются из подтверждённых фактов. Календарь публикаций фактов не создаёт.
    """
    result = ExtractedFinancials()
    if document.kind != "report_content":
        result.quality.append("Материал не содержит подтверждённого содержания отчётности")
        return result
    introduction = "\n".join(pages[:5]).casefold()
    standard = "IFRS" if "мсфо" in introduction or "international financial reporting standards" in introduction else "RAS" if "рсбу" in introduction else ""
    consolidated = False if "неконсолидирован" in introduction or "отдельная финансовая" in introduction else True if "консолидирован" in introduction else None
    if not standard or consolidated is None:
        result.quality.append("Не подтверждены стандарт или область консолидации отчётности")
        return result
    candidates, conflicts = {}, set()
    for page_number, text in enumerate(pages, 1):
        lines = text.splitlines()
        header = "\n".join(lines[:20])
        units = _units(header)
        year_rows = [re.findall(r"\b20\d{2}\b", line) for line in lines[:20]]
        columns = next((list(map(int, row)) for row in year_rows if len(row) in {1, 2} and len(set(row)) == len(row)), [])
        # Годовые даты заголовка могут идти по одной строке перед таблицей.
        two_columns = next((list(map(int, row)) for row in year_rows if len(row) == 2 and len(set(row)) == 2), None)
        columns = two_columns or columns
        if units is None or not columns:
            continue
        scale, currency = units
        for line in lines:
            cells = re.split(r"\s{2,}|\t|\|", line.strip())
            label = cells[0].casefold().replace("ё", "е") if cells else ""
            metric = METRICS.get(label)
            if metric is None:
                continue
            numbers = cells[1:]
            if len(numbers) == len(columns)+1 and "прим." in header.casefold() and re.fullmatch(r"\d{1,3}", numbers[0]):
                numbers = numbers[1:]
            if len(numbers) != len(columns):
                result.quality.append(f"Страница {page_number} · неоднозначные столбцы показателя {metric}")
                continue
            for year, cell in zip(columns, numbers):
                period = _period(header, year, metric in STOCK_METRICS)
                if period is None:
                    result.quality.append(f"Страница {page_number} · период показателя {metric} не подтверждён")
                    continue
                try:
                    value = _number(cell)
                except ValueError:
                    result.quality.append(f"Страница {page_number} · ячейка показателя {metric} не подтверждена")
                    continue
                start, end = map(date.isoformat, period)
                key = (metric, start, end, currency)
                chunk_id = sha256(f"{document.id}:{document.version}:page:{page_number}:{line}".encode()).hexdigest()
                chunk = EvidenceChunk(chunk_id, document.id, document.version, header+"\n"+line,
                                      section="Финансовая отчётность", page=page_number)
                if all(item.id != chunk.id for item in result.chunks):
                    result.chunks.append(chunk)
                fact = FinancialFact(sha256(f"{document.id}:{instrument_id}:{key}".encode()).hexdigest(), instrument_id,
                                     metric, value*scale if value is not None else None, "money", currency, start, end,
                                     document.id, document.version, chunk.id, document.available_at, standard, consolidated)
                if key in candidates and candidates[key].value != fact.value:
                    conflicts.add(key)
                candidates.setdefault(key, fact)
    result.facts = [fact for key, fact in candidates.items() if key not in conflicts]
    if conflicts:
        result.quality.append("Конфликтующие значения одного показателя исключены")
    if not result.facts:
        result.quality.append("Подтверждённые финансовые показатели не извлечены")
    result.quality = list(dict.fromkeys(result.quality))
    return result
