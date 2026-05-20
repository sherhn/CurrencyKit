from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import math
import httpx
from datetime import date
from typing import Optional
import asyncio
import os

app = FastAPI(title="Currency Service", version="1.0.0")

EXCHANGE_API = "https://api.exchangerate-api.com/v4/latest"
HISTORY_API = "https://api.frankfurter.app"

# Количество ретраев и таймаут берутся из переменных окружения
HTTP_RETRIES = int(os.getenv("HTTP_RETRIES", "3"))
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "5.0"))


def make_client() -> httpx.AsyncClient:
    transport = httpx.AsyncHTTPTransport(retries=HTTP_RETRIES)
    return httpx.AsyncClient(transport=transport, timeout=HTTP_TIMEOUT)


async def get_rate(base: str, target: str) -> float:
    """Получить текущий курс валюты через exchangerate-api."""
    async with make_client() as client:
        resp = await client.get(f"{EXCHANGE_API}/{base.upper()}")
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Ошибка получения курса валют")
        data = resp.json()
        rates = data.get("rates", {})
        if target.upper() not in rates:
            raise HTTPException(status_code=404, detail=f"Валюта {target} не найдена")
        return rates[target.upper()]


# 1. Текущий курс
@app.get("/rate/{base}/{target}", summary="Текущий курс валюты")
async def current_rate(base: str, target: str):
    """
    Возвращает текущий обменный курс одной валюты к другой.

    Принимает:
        base   (path) — базовая валюта, ISO 4217, например USD
        target (path) — целевая валюта, ISO 4217, например EUR

    Возвращает:
        base   — базовая валюта (upper)
        target — целевая валюта (upper)
        rate   — текущий курс: сколько единиц target стоит 1 единица base
    """
    rate = await get_rate(base, target)
    return {"base": base.upper(), "target": target.upper(), "rate": rate}


# 2. Конвертация с дополнительными операциями
class ConvertRequest(BaseModel):
    base: str
    target: str
    amount: float
    operation: Optional[str] = None  # nines | ceil | floor


def apply_nines(value: float) -> float:
    """Привести к «ценовым девяткам»: 9.21 → 9.99, 12.50 → 19.99."""
    if value < 1:
        return round(value, 2)
    magnitude = 10 ** math.floor(math.log10(value))
    upper = math.ceil(value / magnitude) * magnitude
    return round(upper - 0.01, 2)


@app.post("/convert", summary="Конвертация с дополнительными операциями")
async def convert(req: ConvertRequest):
    """
    Конвертирует сумму из базовой валюты в целевую и опционально
    применяет округление к результату.

    Принимает (JSON body):
        base      — базовая валюта, ISO 4217
        target    — целевая валюта, ISO 4217
        amount    — сумма в базовой валюте
        operation — необязательно; одно из:
                      "nines"  — привести к «ценовым девяткам» (9.99, 19.99…)
                      "ceil"   — округлить вверх до целого
                      "floor"  — округлить вниз до целого
                      null     — без округления (по умолчанию)

    Возвращает:
        base      — базовая валюта
        target    — целевая валюта
        amount    — исходная сумма
        rate      — курс на момент запроса
        raw       — результат без округления (4 знака)
        operation — применённая операция или null
        result    — итоговое значение после операции
    """
    rate = await get_rate(req.base, req.target)
    raw = req.amount * rate

    result = raw
    if req.operation == "nines":
        result = apply_nines(raw)
    elif req.operation == "ceil":
        result = math.ceil(raw)
    elif req.operation == "floor":
        result = math.floor(raw)
    elif req.operation is not None:
        raise HTTPException(status_code=400, detail="operation: nines | ceil | floor | null")

    return {
        "base": req.base.upper(),
        "target": req.target.upper(),
        "amount": req.amount,
        "rate": rate,
        "raw": round(raw, 4),
        "operation": req.operation,
        "result": result,
    }


# 3. Bid / Ask спред
@app.get("/spread/{base}/{target}", summary="Bid/Ask спред")
async def spread(base: str, target: str, spread_pct: float = 0.1):
    """
    Рассчитывает bid и ask вокруг текущего mid-курса.

    Принимает:
        base       (path)  — базовая валюта, ISO 4217
        target     (path)  — целевая валюта, ISO 4217
        spread_pct (query) — полный спред в процентах, по умолчанию 0.1
                             (bid = mid × (1 − spread/2), ask = mid × (1 + spread/2))

    Возвращает:
        base       — базовая валюта
        target     — целевая валюта
        mid        — средний курс (6 знаков)
        bid        — курс покупки (6 знаков)
        ask        — курс продажи (6 знаков)
        spread_pct — полный спред в процентах
        spread_abs — абсолютный спред (ask − bid, 6 знаков)
    """
    rate = await get_rate(base, target)
    half = spread_pct / 100 / 2
    bid = rate * (1 - half)
    ask = rate * (1 + half)
    return {
        "base": base.upper(),
        "target": target.upper(),
        "mid": round(rate, 6),
        "bid": round(bid, 6),
        "ask": round(ask, 6),
        "spread_pct": spread_pct,
        "spread_abs": round(ask - bid, 6),
    }


# 4. Исторический курс на дату
@app.get("/history/{base}/{target}", summary="Конвертация по историческому курсу")
async def history_rate(base: str, target: str, on_date: date, amount: float = 1.0):
    """
    Возвращает курс и результат конвертации на конкретную дату.
    Источник данных: frankfurter.app (ЕЦБ).

    Принимает:
        base    (path)  — базовая валюта, ISO 4217
        target  (path)  — целевая валюта, ISO 4217
        on_date (query) — дата в формате YYYY-MM-DD (обязательно)
        amount  (query) — сумма в базовой валюте, по умолчанию 1.0

    Возвращает:
        base      — базовая валюта
        target    — целевая валюта
        on_date   — запрошенная дата
        amount    — исходная сумма
        rate      — курс на указанную дату (6 знаков)
        converted — результат конвертации (4 знака)
    """
    async with make_client() as client:
        url = f"{HISTORY_API}/{on_date}?from={base.upper()}&to={target.upper()}&amount={amount}"
        resp = await client.get(url)
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Ошибка получения исторического курса")
        data = resp.json()
        if "rates" not in data or target.upper() not in data["rates"]:
            raise HTTPException(status_code=404, detail="Данные не найдены")
        converted = data["rates"][target.upper()]
        rate = converted / amount
    return {
        "base": base.upper(),
        "target": target.upper(),
        "on_date": str(on_date),
        "amount": amount,
        "rate": round(rate, 6),
        "converted": round(converted, 4),
    }


# 5. Динамика курса за период
@app.get("/history/{base}/{target}/range", summary="Динамика курса за период")
async def history_range(
    base: str,
    target: str,
    from_date: date,
    to_date: date,
    amount: float = 1.0,
):
    """
    Возвращает ряд курсов за период from_date..to_date включительно
    и агрегированную статистику по нему.
    Выходные и праздничные дни ЕЦБ в ответе отсутствуют — это ожидаемо.
    Источник данных: frankfurter.app (ЕЦБ).

    Принимает:
        base      (path)  — базовая валюта, ISO 4217
        target    (path)  — целевая валюта, ISO 4217
        from_date (query) — начало периода, YYYY-MM-DD (обязательно)
        to_date   (query) — конец периода, YYYY-MM-DD (обязательно)
        amount    (query) — сумма в базовой валюте, по умолчанию 1.0

    Возвращает:
        base      — базовая валюта
        target    — целевая валюта
        from_date — начало периода
        to_date   — конец периода
        amount    — исходная сумма
        points    — количество торговых дней в ответе
        min_rate  — минимальный курс за период
        max_rate  — максимальный курс за период
        avg_rate  — средний курс за период (6 знаков)
        data      — массив объектов { date, rate, converted } по каждому дню
    """
    if from_date > to_date:
        raise HTTPException(status_code=400, detail="from_date должна быть раньше to_date")

    async with make_client() as client:
        url = (
            f"{HISTORY_API}/{from_date}..{to_date}"
            f"?from={base.upper()}&to={target.upper()}&amount={amount}"
        )
        resp = await client.get(url)
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Ошибка получения исторических данных")
        data = resp.json()

    raw_rates = data.get("rates", {})
    if not raw_rates:
        raise HTTPException(status_code=404, detail="Данные за указанный период не найдены")

    points = []
    for day, currencies in sorted(raw_rates.items()):
        converted = currencies.get(target.upper())
        if converted is None:
            continue
        points.append({
            "date": day,
            "rate": round(converted / amount, 6),
            "converted": round(converted, 4),
        })

    if not points:
        raise HTTPException(status_code=404, detail=f"Валюта {target} не найдена в ответе")

    rates_only = [p["rate"] for p in points]
    return {
        "base": base.upper(),
        "target": target.upper(),
        "from_date": str(from_date),
        "to_date": str(to_date),
        "amount": amount,
        "points": len(points),
        "min_rate": min(rates_only),
        "max_rate": max(rates_only),
        "avg_rate": round(sum(rates_only) / len(rates_only), 6),
        "data": points,
    }


# 6. Арбитражный треугольник
@app.get("/arbitrage", summary="Арбитражный треугольник A→B→C→A")
async def arbitrage(a: str, b: str, c: str, amount: float = 1000.0):
    """
    Проверяет, есть ли прибыль при последовательной конвертации A→B→C→A.
    Все три курса запрашиваются параллельно.

    Принимает:
        a      (query) — первая валюта, ISO 4217
        b      (query) — вторая валюта, ISO 4217
        c      (query) — третья валюта, ISO 4217
        amount (query) — стартовая сумма в валюте A, по умолчанию 1000.0

    Возвращает:
        path         — цепочка конвертации, например USD→EUR→GBP→USD
        start_amount — исходная сумма
        after_ab     — сумма после A→B (4 знака)
        after_bc     — сумма после B→C (4 знака)
        after_ca     — сумма после C→A (4 знака)
        profit       — абсолютная прибыль/убыток (4 знака)
        profit_pct   — прибыль в процентах от start_amount (4 знака)
        is_profitable— true, если прибыль положительная
        rates        — словарь с тремя использованными курсами
    """
    rate_ab, rate_bc, rate_ca = await asyncio.gather(
        get_rate(a, b),
        get_rate(b, c),
        get_rate(c, a),
    )

    step1 = amount * rate_ab
    step2 = step1 * rate_bc
    step3 = step2 * rate_ca

    profit = step3 - amount
    profit_pct = (profit / amount) * 100

    return {
        "path": f"{a.upper()}→{b.upper()}→{c.upper()}→{a.upper()}",
        "start_amount": amount,
        "after_ab": round(step1, 4),
        "after_bc": round(step2, 4),
        "after_ca": round(step3, 4),
        "profit": round(profit, 4),
        "profit_pct": round(profit_pct, 4),
        "is_profitable": profit > 0,
        "rates": {
            f"{a.upper()}/{b.upper()}": rate_ab,
            f"{b.upper()}/{c.upper()}": rate_bc,
            f"{c.upper()}/{a.upper()}": rate_ca,
        },
    }


# 7. Пакетная конвертация
@app.get("/batch", summary="Пакетная конвертация одной базовой валюты в несколько целевых")
async def batch_convert(base: str, targets: str, amount: float = 1.0):
    """
    Конвертирует одну сумму сразу в несколько валют параллельно.
    Если отдельная валюта недоступна, остальные результаты всё равно возвращаются.

    Принимает:
        base    (query) — базовая валюта, ISO 4217
        targets (query) — целевые валюты через запятую, например EUR,GBP,JPY,CHF
                          максимум 30 валют за запрос
        amount  (query) — сумма в базовой валюте, по умолчанию 1.0

    Возвращает:
        base        — базовая валюта
        amount      — исходная сумма
        conversions — словарь { ВАЛЮТА: { rate, converted, error } } для каждой целевой валюты;
                      поля rate и converted равны null при ошибке
        summary     — итог: total, successful, failed, errors (словарь с описаниями ошибок)
    """
    target_list = [t.strip().upper() for t in targets.split(",") if t.strip()]
    if not target_list:
        raise HTTPException(status_code=400, detail="Укажите хотя бы одну валюту в targets")
    if len(target_list) > 30:
        raise HTTPException(status_code=400, detail="Максимум 30 валют за запрос")

    async def safe_get_rate(target: str):
        try:
            rate = await get_rate(base, target)
            return target, {"rate": rate, "converted": round(amount * rate, 4), "error": None}
        except HTTPException as e:
            return target, {"rate": None, "converted": None, "error": e.detail}

    results = await asyncio.gather(*[safe_get_rate(t) for t in target_list])

    conversions = {target: info for target, info in results}
    successful = {k: v for k, v in conversions.items() if v["error"] is None}
    failed = {k: v["error"] for k, v in conversions.items() if v["error"] is not None}

    return {
        "base": base.upper(),
        "amount": amount,
        "conversions": conversions,
        "summary": {
            "total": len(target_list),
            "successful": len(successful),
            "failed": len(failed),
            "errors": failed if failed else None,
        },
    }


@app.get("/", summary="Список эндпоинтов и текущая конфигурация")
def root():
    return {
        "service": "Currency Service",
        "config": {
            "http_retries": HTTP_RETRIES,
            "http_timeout_sec": HTTP_TIMEOUT,
        },
        "endpoints": {
            "GET /rate/{base}/{target}": "Текущий курс",
            "POST /convert": "Конвертация (nines/ceil/floor)",
            "GET /spread/{base}/{target}": "Bid/Ask спред",
            "GET /history/{base}/{target}?on_date=YYYY-MM-DD": "Исторический курс на дату",
            "GET /history/{base}/{target}/range?from_date=YYYY-MM-DD&to_date=YYYY-MM-DD": "Динамика за период",
            "GET /batch?base=USD&targets=EUR,GBP,JPY": "Пакетная конвертация",
            "GET /arbitrage?a=USD&b=EUR&c=GBP": "Арбитражный треугольник",
        },
    }