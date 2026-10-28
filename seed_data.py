"""Seed MongoDB Atlas with sample products and 14 days of competitor price history.

Usage:  python seed_data.py
Warning: this DELETES the existing "products" and "competitor_prices" collections.
"""
import math
import random
from datetime import datetime, timedelta, timezone

from database import get_db

DAYS = 14
VENDORS = {"MegaMart": 0.99, "PriceHub": 1.02, "ShopZone": 0.97, "BuyRight": 1.01}

# (title, category, brand, typical market price, our price vs market, cost as share of market)
CATALOG = [
    ("AirPulse Pro Wireless Earbuds", "Audio", "AirPulse", 129.00, 1.18, 0.58),
    ("NovaBeat Studio Headphones", "Audio", "NovaBeat", 219.00, 0.99, 0.62),
    ("Lumen 27\" 4K Monitor", "Displays", "Lumen", 389.00, 0.86, 0.66),
    ("Lumen 24\" Full HD Monitor", "Displays", "Lumen", 159.00, 1.01, 0.64),
    ("Stride X2 Running Shoes", "Footwear", "Stride", 94.00, 1.14, 0.45),
    ("Stride Trail Hiker Boots", "Footwear", "Stride", 139.00, 0.97, 0.52),
    ("BrewMate Espresso Machine", "Kitchen", "BrewMate", 249.00, 0.88, 0.68),
    ("BrewMate Burr Grinder", "Kitchen", "BrewMate", 79.00, 1.02, 0.85),
    ("ZenDesk Standing Desk", "Furniture", "ZenDesk", 429.00, 1.12, 0.60),
    ("ZenDesk Ergonomic Chair", "Furniture", "ZenDesk", 299.00, 1.00, 0.63),
    ("Volt 65W GaN Charger", "Accessories", "Volt", 39.00, 0.90, 0.50),
    ("Volt 20,000 mAh Power Bank", "Accessories", "Volt", 49.00, 1.03, 0.55),
]


def main() -> None:
    rng = random.Random(42)
    db = get_db()

    print("Clearing existing sample collections...")
    db.products.delete_many({})
    db.competitor_prices.delete_many({})

    products = []
    for title, category, brand, market, our_factor, cost_share in CATALOG:
        products.append(
            {
                "title": title,
                "category": category,
                "brand": brand,
                "sku": f"{brand[:3].upper()}-{rng.randint(1000, 9999)}",
                "base_cost": round(market * cost_share, 2),
                "current_price": round(market * our_factor, 2),
                "_market": market,
            }
        )

    market_by_title = {p["title"]: p.pop("_market") for p in products}
    result = db.products.insert_many(products)
    print(f"Inserted {len(result.inserted_ids)} products.")

    db.price_history.delete_many({})
    history = []
    reasons = ["Manual edit", "Applied suggestion", "Seasonal promotion", "Supplier cost change"]
    for pid, p in zip(result.inserted_ids, products):
        price, when = p["current_price"], datetime.now(timezone.utc)
        for _ in range(rng.randint(2, 3)):  # walk backwards in time, newest change first
            old = round(price * rng.uniform(0.94, 1.08), 2)
            when -= timedelta(days=rng.randint(1, 4), hours=rng.randint(0, 20))
            history.append({"product_id": pid, "title": p["title"], "old_price": old, "new_price": price,
                            "reason": rng.choice(reasons), "at": when})
            price = old
    db.price_history.insert_many(history)
    db.price_history.create_index([("at", -1)])
    print(f"Inserted {len(history)} price history entries.")

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    logs = []
    for product_id, product in zip(result.inserted_ids, products):
        market = market_by_title[product["title"]]
        drift = rng.uniform(-0.05, 0.03)  # market drifts over the 14 days
        phase = rng.uniform(0, math.pi)
        for day in range(DAYS):
            date = today - timedelta(days=DAYS - 1 - day)
            trend = 1 + drift * (day / (DAYS - 1)) + 0.008 * math.sin(day / 2 + phase)
            for vendor, bias in VENDORS.items():
                price = market * bias * trend * (1 + rng.uniform(-0.012, 0.012))
                logs.append(
                    {"product_id": product_id, "vendor": vendor, "price": round(price, 2), "date": date}
                )

    db.competitor_prices.insert_many(logs)
    db.competitor_prices.create_index([("product_id", 1), ("date", -1)])
    db.competitor_prices.create_index([("date", -1)])
    print(f"Inserted {len(logs)} competitor price logs ({len(VENDORS)} vendors x {DAYS} days).")
    print("Done. Start the app with: uvicorn main:app --reload")


if __name__ == "__main__":
    main()
