"""E-Commerce Dynamic Pricing & Competitor Intelligence Platform (FastAPI)."""
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from pymongo import UpdateOne
from pymongo.errors import PyMongoError

from database import close_client, get_db

APP_NAME = "Pricing Intelligence"
BAND_PCT = 5.0  # +/- band around the market average that counts as "Competitive"
UNDERCUT_TARGET = 0.98  # when repricing, aim 2% below the market average

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    close_client()


app = FastAPI(title=APP_NAME, lifespan=lifespan)


def _clean(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> JSON-safe list of dicts (NaN becomes None)."""
    return df.astype(object).where(df.notna(), None).to_dict("records")


def _parse_object_id(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=400, detail="Invalid product id.")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"app_name": APP_NAME})


@app.get("/api/analytics")
def analytics(target_margin: float = Query(20.0, ge=0, le=90, description="Target profit margin in percent")):
    try:
        db = get_db()
        products = list(db.products.find())
        # Latest price per (product, vendor), then averaged per product.
        pipeline = [
            {"$sort": {"date": -1}},
            {"$group": {"_id": {"p": "$product_id", "v": "$vendor"}, "price": {"$first": "$price"}}},
            {
                "$group": {
                    "_id": "$_id.p",
                    "market_avg": {"$avg": "$price"},
                    "market_min": {"$min": "$price"},
                    "market_max": {"$max": "$price"},
                    "vendors": {"$sum": 1},
                }
            },
        ]
        stats = list(db.competitor_prices.aggregate(pipeline))
    except (PyMongoError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}")

    margin = target_margin / 100.0
    kpis = {
        "total_products": 0,
        "avg_market_discount_pct": 0.0,
        "items_needing_review": 0,
        "target_margin_pct": round(target_margin, 1),
        "overpriced": 0,
        "undercut": 0,
        "competitive": 0,
    }
    if not products:
        return {"kpis": kpis, "products": []}

    df = pd.DataFrame(
        [
            {
                "id": str(p["_id"]),
                "title": p.get("title", ""),
                "category": p.get("category", ""),
                "brand": p.get("brand", ""),
                "base_cost": float(p.get("base_cost", 0)),
                "current_price": float(p.get("current_price", 0)),
            }
            for p in products
        ]
    )
    stats_df = pd.DataFrame(stats)
    if stats_df.empty:
        stats_df = pd.DataFrame(columns=["id", "market_avg", "market_min", "market_max", "vendors"])
    else:
        stats_df["id"] = stats_df["_id"].astype(str)
        stats_df = stats_df.drop(columns=["_id"])
    df = df.merge(stats_df, on="id", how="left")

    has_data = df["market_avg"].notna()
    df["diff_pct"] = (df["current_price"] - df["market_avg"]) / df["market_avg"] * 100

    df["status"] = np.select(
        [~has_data, df["diff_pct"] > BAND_PCT, df["diff_pct"] < -BAND_PCT],
        ["No Data", "Overpriced", "Undercut"],
        default="Competitive",
    )

    # Suggested price: never below the price that yields the target margin.
    df["margin_floor"] = df["base_cost"] / (1 - margin)
    target_price = df["market_avg"] * UNDERCUT_TARGET
    reprice = np.maximum(df["margin_floor"], target_price.fillna(df["margin_floor"]))
    keep_current = (df["status"] == "Competitive") & (df["current_price"] >= df["margin_floor"])
    df["suggested_price"] = np.where(keep_current, df["current_price"], reprice)
    df["margin_bound"] = has_data & (df["margin_floor"] > target_price) & ~keep_current
    df["below_target_margin"] = df["current_price"] < df["margin_floor"]

    df["current_margin_pct"] = (df["current_price"] - df["base_cost"]) / df["current_price"] * 100
    df["suggested_margin_pct"] = (df["suggested_price"] - df["base_cost"]) / df["suggested_price"] * 100
    df["price_change"] = df["suggested_price"] - df["current_price"]

    needs_review = df["status"].isin(["Overpriced", "Undercut"]) | df["below_target_margin"]
    kpis.update(
        total_products=int(len(df)),
        avg_market_discount_pct=round(float(-df["diff_pct"].mean()), 2) if has_data.any() else 0.0,
        items_needing_review=int(needs_review.sum()),
        overpriced=int((df["status"] == "Overpriced").sum()),
        undercut=int((df["status"] == "Undercut").sum()),
        competitive=int((df["status"] == "Competitive").sum()),
    )

    money = ["base_cost", "current_price", "market_avg", "market_min", "market_max",
             "margin_floor", "suggested_price", "price_change"]
    pct = ["diff_pct", "current_margin_pct", "suggested_margin_pct"]
    df[money + pct] = df[money + pct].round(2)
    df = df.sort_values("title").reset_index(drop=True)
    return {"kpis": kpis, "products": _clean(df)}


@app.get("/api/chart-data/{product_id}")
def chart_data(product_id: str):
    oid = _parse_object_id(product_id)
    try:
        db = get_db()
        product = db.products.find_one({"_id": oid})
        if product is None:
            raise HTTPException(status_code=404, detail="Product not found.")
        logs = list(
            db.competitor_prices.find({"product_id": oid}, {"_id": 0, "vendor": 1, "price": 1, "date": 1})
        )
    except (PyMongoError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}")

    payload = {
        "product": {
            "id": product_id,
            "title": product.get("title", ""),
            "base_cost": float(product.get("base_cost", 0)),
            "current_price": float(product.get("current_price", 0)),
        },
        "labels": [],
        "datasets": [],
        "market_avg": [],
        "forecast": None,
    }
    if not logs:
        return payload

    df = pd.DataFrame(logs)
    df["day"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    pivot = df.pivot_table(index="day", columns="vendor", values="price", aggfunc="mean").sort_index()
    payload["labels"] = [str(d) for d in pivot.index]
    payload["datasets"] = [
        {"vendor": str(v), "data": [None if pd.isna(x) else round(float(x), 2) for x in pivot[v]]}
        for v in pivot.columns
    ]
    payload["market_avg"] = [round(float(x), 2) for x in pivot.mean(axis=1)]
    payload["forecast"] = _forecast(list(pivot.index), np.array(payload["market_avg"], dtype=float))
    return payload


# --------------------------------------------------------------------------
# CRUD: products, competitor price logs, bulk repricing
# --------------------------------------------------------------------------
class ProductIn(BaseModel):
    title: str = Field(..., min_length=2, max_length=120)
    category: str = Field(..., min_length=1, max_length=60)
    brand: str = Field(..., min_length=1, max_length=60)
    base_cost: float = Field(..., gt=0, le=10_000_000)
    current_price: float = Field(..., gt=0, le=10_000_000)


class PriceLogIn(BaseModel):
    vendor: str = Field(..., min_length=1, max_length=60)
    price: float = Field(..., gt=0, le=10_000_000)


def _guard(fn):
    """Run a DB operation and translate connection errors into HTTP 503."""
    try:
        return fn()
    except HTTPException:
        raise
    except (PyMongoError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}")


def _product_doc(body: ProductIn) -> dict:
    return {
        "title": body.title.strip(),
        "category": body.category.strip(),
        "brand": body.brand.strip(),
        "base_cost": round(body.base_cost, 2),
        "current_price": round(body.current_price, 2),
    }


@app.post("/api/products", status_code=201)
def create_product(body: ProductIn):
    def run():
        db = get_db()
        doc = _product_doc(body)
        oid = db.products.insert_one(doc).inserted_id
        _log_change(db, oid, doc["title"], None, doc["current_price"], "Product created")
        return {"id": str(oid)}

    return _guard(run)


@app.put("/api/products/{product_id}")
def update_product(product_id: str, body: ProductIn, reason: str = Query("Manual edit", max_length=60)):
    oid = _parse_object_id(product_id)

    def run():
        db = get_db()
        old = db.products.find_one({"_id": oid})
        if old is None:
            raise HTTPException(status_code=404, detail="Product not found.")
        doc = _product_doc(body)
        db.products.update_one({"_id": oid}, {"$set": doc})
        _log_change(db, oid, doc["title"], old.get("current_price"), doc["current_price"], reason)
        return {"updated": True}

    return _guard(run)


@app.delete("/api/products/{product_id}")
def delete_product(product_id: str):
    oid = _parse_object_id(product_id)

    def run():
        db = get_db()
        if db.products.delete_one({"_id": oid}).deleted_count == 0:
            raise HTTPException(status_code=404, detail="Product not found.")
        removed = db.competitor_prices.delete_many({"product_id": oid}).deleted_count
        return {"deleted": True, "price_logs_removed": removed}

    return _guard(run)


@app.post("/api/products/{product_id}/competitor-prices", status_code=201)
def log_competitor_price(product_id: str, body: PriceLogIn):
    """Record today's price for a vendor (re-logging the same vendor today overwrites it)."""
    oid = _parse_object_id(product_id)

    def run():
        db = get_db()
        if db.products.find_one({"_id": oid}, {"_id": 1}) is None:
            raise HTTPException(status_code=404, detail="Product not found.")
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        vendor = body.vendor.strip()
        db.competitor_prices.update_one(
            {"product_id": oid, "vendor": vendor, "date": today},
            {"$set": {"price": round(body.price, 2)}},
            upsert=True,
        )
        return {"logged": True, "vendor": vendor}

    return _guard(run)


@app.post("/api/apply-suggestions")
def apply_suggestions(target_margin: float = Query(20.0, ge=0, le=90), reason: str = Query("Applied suggestion (bulk)", max_length=60)):
    """Set every product's current price to its suggested price (and record the changes)."""
    rows = [
        r for r in analytics(target_margin)["products"]
        if r["status"] != "No Data" and abs(r["price_change"]) >= 0.01
    ]

    def run():
        if not rows:
            return
        db = get_db()
        db.products.bulk_write(
            [UpdateOne({"_id": ObjectId(r["id"])}, {"$set": {"current_price": r["suggested_price"]}}) for r in rows]
        )
        db.price_history.insert_many(
            [_history_doc(ObjectId(r["id"]), r["title"], r["current_price"], r["suggested_price"], reason) for r in rows]
        )

    _guard(run)
    return {"updated": len(rows)}


# --------------------------------------------------------------------------
# Price history (audit trail)
# --------------------------------------------------------------------------
def _history_doc(oid, title, old, new, reason):
    return {
        "product_id": oid,
        "title": title,
        "old_price": None if old is None else float(old),
        "new_price": float(new),
        "reason": reason,
        "at": datetime.now(timezone.utc),
    }


def _log_change(db, oid, title, old, new, reason):
    """Record a price change; unchanged prices are not logged."""
    if old is None or abs(float(old) - float(new)) >= 0.005:
        db.price_history.insert_one(_history_doc(oid, title, old, new, reason))


@app.get("/api/price-history")
def price_history(limit: int = Query(100, ge=1, le=500)):
    docs = _guard(lambda: list(get_db().price_history.find().sort("at", -1).limit(limit)))
    rows = []
    for d in docs:
        old, new = d.get("old_price"), d["new_price"]
        rows.append(
            {
                "id": str(d["_id"]),
                "product_id": str(d["product_id"]),
                "title": d.get("title", ""),
                "old_price": old,
                "new_price": new,
                "change_pct": None if not old else round((new - old) / old * 100, 2),
                "reason": d.get("reason", ""),
                "at": d["at"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )
    return {"rows": rows}


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------
ALERT_MOVE_PCT = 3.0  # competitor day-over-day move that triggers an alert
_SEVERITY = {"critical": 0, "warning": 1, "info": 2}


@app.get("/api/alerts")
def alerts(target_margin: float = Query(20.0, ge=0, le=90)):
    rows = analytics(target_margin)["products"]
    out = []

    def add(sev, kind, r, msg):
        out.append({"severity": sev, "type": kind, "product_id": r["id"], "product": r["title"], "message": msg})

    for r in rows:
        if r["below_target_margin"]:
            add("critical", "Below target margin", r,
                f"Sold at ${r['current_price']:.2f}, a {r['current_margin_pct']:.1f}% margin. "
                f"You need at least ${r['margin_floor']:.2f} to reach {target_margin:g}%.")
        if r["status"] == "Overpriced":
            add("warning", "Overpriced", r,
                f"{abs(r['diff_pct']):.1f}% above the market average of ${r['market_avg']:.2f}. Suggested price: ${r['suggested_price']:.2f}.")
        elif r["status"] == "Undercut":
            add("warning", "Priced below market", r,
                f"{abs(r['diff_pct']):.1f}% below the market average of ${r['market_avg']:.2f}. You could earn more at ${r['suggested_price']:.2f}.")
        elif r["status"] == "No Data":
            add("info", "No competitor data", r, "Log at least one competitor price to analyse this product.")

    since = datetime.now(timezone.utc) - timedelta(days=7)
    logs = _guard(lambda: list(get_db().competitor_prices.find({"date": {"$gte": since}}, {"_id": 0})))
    if logs:
        df = pd.DataFrame(logs).sort_values("date")
        keys = ["product_id", "vendor"]
        df["chg"] = df.groupby(keys, sort=False)["price"].pct_change() * 100
        last = df.groupby(keys, sort=False).tail(1)
        by_id = {r["id"]: r for r in rows}
        for _, mv in last[last["chg"].abs() >= ALERT_MOVE_PCT].iterrows():
            r = by_id.get(str(mv["product_id"]))
            if r is None:
                continue
            drop = mv["chg"] < 0
            add("warning" if drop else "info", "Competitor price drop" if drop else "Competitor price rise", r,
                f"{mv['vendor']} {'cut' if drop else 'raised'} its price by {abs(mv['chg']):.1f}% to ${mv['price']:.2f}.")

    out.sort(key=lambda a: _SEVERITY[a["severity"]])
    return {"alerts": out}


# --------------------------------------------------------------------------
# Forecast (linear regression on the daily market average)
# --------------------------------------------------------------------------
def _forecast(days: list, y: np.ndarray, horizon: int = 7):
    if len(y) < 3:
        return None
    x = np.arange(len(y))
    slope, intercept = np.polyfit(x, y, 1)
    fitted = slope * x + intercept
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1 - float(np.sum((y - fitted) ** 2)) / ss_tot if ss_tot > 0 else 0.0
    future = np.maximum(slope * np.arange(len(y), len(y) + horizon) + intercept, 0.01)
    last_day = pd.to_datetime(days[-1])
    pct_per_day = slope / float(y.mean()) * 100
    return {
        "labels": [(last_day + pd.Timedelta(days=i + 1)).strftime("%Y-%m-%d") for i in range(horizon)],
        "market_avg": [round(float(v), 2) for v in future],
        "slope_per_day": round(float(slope), 3),
        "r2": round(r2, 2),
        "direction": "falling" if pct_per_day < -0.15 else "rising" if pct_per_day > 0.15 else "stable",
    }


# --------------------------------------------------------------------------
# What-if simulator (constant-elasticity demand: units = base_units * (price / current_price) ^ elasticity)
# --------------------------------------------------------------------------
@app.get("/api/simulate/{product_id}")
def simulate(
    product_id: str,
    new_price: float = Query(..., gt=0, le=10_000_000),
    units: float = Query(50, ge=1, le=1_000_000, description="Baseline units sold per day at the current price"),
    elasticity: float = Query(-1.5, ge=-5, le=-0.1),
):
    oid = _parse_object_id(product_id)
    product = _guard(lambda: get_db().products.find_one({"_id": oid}))
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found.")
    p0, cost = float(product["current_price"]), float(product["base_cost"])

    def outcome(p: float) -> dict:
        q = units * (p / p0) ** elasticity
        return {"price": round(p, 2), "units": round(q, 1), "revenue": round(p * q, 2), "profit": round((p - cost) * q, 2)}

    base, scen = outcome(p0), outcome(new_price)
    curve = [outcome(float(p)) for p in np.linspace(p0 * 0.6, p0 * 1.6, 41)]
    return {
        "product": {"id": product_id, "title": product.get("title", ""), "current_price": p0, "base_cost": cost},
        "baseline": base,
        "scenario": scen,
        "curve": curve,
        "optimal": max(curve, key=lambda o: o["profit"]),
    }


# --------------------------------------------------------------------------
# CSV export
# --------------------------------------------------------------------------
def _csv(df: pd.DataFrame, filename: str) -> Response:
    return Response(
        df.to_csv(index=False),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/export/products.csv")
def export_products(target_margin: float = Query(20.0, ge=0, le=90)):
    cols = ["title", "brand", "category", "base_cost", "current_price", "market_avg", "market_min", "market_max",
            "diff_pct", "status", "margin_floor", "suggested_price", "price_change", "current_margin_pct", "suggested_margin_pct"]
    df = pd.DataFrame(analytics(target_margin)["products"], columns=cols)
    return _csv(df, f"pricing_analysis_{target_margin:g}pct_margin.csv")


@app.get("/api/export/price-history.csv")
def export_history():
    cols = ["at", "title", "old_price", "new_price", "change_pct", "reason"]
    return _csv(pd.DataFrame(price_history(500)["rows"], columns=cols), "price_history.csv")
