# E-Commerce Dynamic Pricing & Competitor Intelligence Platform

FastAPI + MongoDB Atlas + Pandas backend with a Tailwind / Chart.js dashboard.
It compares your prices with the latest competitor prices, flags products that are out of line, and suggests prices that still meet your target profit margin.

## Prerequisites
- Python 3.10 or newer
- A MongoDB Atlas cluster (the free M0 tier works)

## 1. Install dependencies
```bash
cd ecommerce-pricing-platform
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Configure MongoDB Atlas
1. In Atlas, create a database user (Database Access) and allow your IP address (Network Access).
2. Click **Connect > Drivers** on your cluster and copy the connection string.
3. Create your `.env` file and fill it in:
```bash
cp .env.example .env            # Windows: copy .env.example .env
```
```
MONGO_URI=mongodb+srv://<username>:<password>@<cluster-url>/?retryWrites=true&w=majority
DB_NAME=pricing_platform
```
If your password has special characters, URL-encode them.

## 3. Seed the database
```bash
python seed_data.py
```
This clears the `products` and `competitor_prices` collections and inserts 12 products plus 14 days of prices from 4 vendors (672 logs).

## 4. Run the server
```bash
uvicorn main:app --reload
```
Open http://127.0.0.1:8000.

## How the pricing logic works
- **Market average**: the latest price from each competitor, averaged per product.
- **Status**: `Overpriced` if your price is more than 5% above the market average, `Undercut` if more than 5% below it, otherwise `Competitive`. `No Data` means no competitor prices exist.
- **Margin floor**: `base_cost / (1 - target_margin)`.
- **Suggested price**: kept as is for competitive products that meet the margin; otherwise 2% below the market average, but never below the margin floor (flagged *margin-bound* when the floor wins).
- **Items needing review**: products that are Overpriced or Undercut, or priced below the margin floor.
- **Market average discount**: average of how far below the market average your prices sit (positive = cheaper than the market).

Tune `BAND_PCT` and `UNDERCUT_TARGET` at the top of `main.py`.

## API
- `GET /` renders the dashboard.
- `GET /api/analytics?target_margin=20` returns KPIs and per-product analytics (`target_margin` is a percent from 0 to 90).
- `GET /api/chart-data/{product_id}` returns 14-day price history per competitor, ready for Chart.js.
- `GET /docs` is the interactive Swagger UI.

## Project structure
```
ecommerce-pricing-platform/
├── .env.example
├── requirements.txt
├── README.md
├── database.py      # Atlas connection
├── seed_data.py     # sample data
├── main.py          # FastAPI app and analytics
└── templates/
    └── index.html   # dashboard
```

## Editing data from the dashboard (CRUD)
Open **Product catalog** to add, edit, delete and reprice products, or log a competitor’s price for today. Everything is saved to MongoDB.
- `POST /api/products` creates a product.
- `PUT /api/products/{id}` updates it.
- `DELETE /api/products/{id}` deletes it and its price history.
- `POST /api/products/{id}/competitor-prices` logs today’s price for a vendor (body: `vendor`, `price`).
- `POST /api/apply-suggestions?target_margin=20` sets every current price to its suggested price.

A new product shows **No Data** until you log at least one competitor price for it.

## Analytics and monitoring features
- **Alerts**: flags products below the target margin, overpriced or underpriced products, and competitor moves of 3% or more since their previous log (`GET /api/alerts`). Log a competitor price 3% or more different from yesterday’s to see one.
- **Price history**: every price change (manual edit, applied suggestion, simulator, new product) is saved in the `price_history` collection with the reason (`GET /api/price-history`). History is kept even if a product is deleted.
- **Export to CSV**: download the pricing analysis (`/api/export/products.csv?target_margin=20`) or the price history (`/api/export/price-history.csv`).
- **Price forecast**: a linear regression (NumPy `polyfit`) on the 14-day market average projects the next 7 days, shown as a dashed purple line with the slope and R² (`forecast` in `/api/chart-data/{id}`).
- **What-if simulator**: assumes constant-elasticity demand, `units = base_units × (new_price / current_price) ^ elasticity`. Revenue is `price × units` and profit is `(price − cost) × units`. It shows the effect of a new price and the profit-maximising price within 60% to 160% of today’s price (`GET /api/simulate/{id}?new_price=&units=&elasticity=`). Elasticity and daily units are assumptions you set, not values learned from sales data.

Re-run `python seed_data.py` once to create sample price history.
