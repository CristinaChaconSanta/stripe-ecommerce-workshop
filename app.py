import os
from pathlib import Path

import stripe
from flask import Flask, abort, jsonify, request, send_from_directory
from werkzeug.utils import safe_join

ROOT = Path(__file__).resolve().parent


def _read_env_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    return raw.decode("utf-8", errors="replace")


def load_dotenv_file() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw in _read_env_text(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and val:
            os.environ[key] = val


load_dotenv_file()

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY") or os.environ.get("stripe_clave_privada")
if not stripe.api_key:
    raise RuntimeError("Define STRIPE_SECRET_KEY o stripe_clave_privada en .env")

# Versión alineada con el SDK de Stripe (evita fallos raros en Checkout con cadenas no publicadas).
app = Flask(__name__)


def _stripe_error_response(exc: stripe.StripeError, status: int) -> tuple:
    message = str(exc)
    extra: dict = {}
    jb = getattr(exc, "json_body", None)
    if isinstance(jb, dict):
        err = jb.get("error")
        if isinstance(err, dict) and err.get("message"):
            message = err["message"]
        if isinstance(err, dict):
            if err.get("code"):
                extra["stripe_code"] = err["code"]
            if err.get("type"):
                extra["stripe_type"] = err["type"]
            if err.get("doc_url"):
                extra["stripe_doc"] = err["doc_url"]
    payload = {"error": message or "Error de Stripe"}
    payload.update(extra)
    return jsonify(payload), status


def _safe_cancel_path(path: str | None) -> str:
    allowed = ("/carrito.html", "/productos.html")
    if path and path in allowed:
        return path
    return "/carrito.html"


def _new_checkout_session(
    mode: str, line_items: list[dict], cancel_url: str
) -> stripe.checkout.Session:
    base = _checkout_base_url()
    return stripe.checkout.Session.create(
        mode=mode,
        line_items=line_items,
        success_url=f"{base}/checkout-success.html?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=cancel_url,
    )


def _recurring_suffix(price: stripe.Price) -> str:
    if price.type != "recurring" or not price.recurring:
        return ""
    interval = getattr(price.recurring, "interval", None) or ""
    return {
        "month": "/mes",
        "year": "/año",
        "week": "/sem",
        "day": "/día",
    }.get(interval, "")


def _format_money(amount_cents: int, currency: str) -> str:
    cur = (currency or "usd").upper()
    major = amount_cents / 100
    if cur == "USD":
        return f"${major:,.2f}"
    return f"{major:,.2f} {cur}"


def _product_payload(prod: stripe.Product, price: stripe.Price) -> dict:
    if price.unit_amount is None:
        return {}
    images = list(prod.images or [])
    return {
        "id": prod.id,
        "priceId": price.id,
        "name": prod.name or "Sin nombre",
        "description": (prod.description or "").strip(),
        "amountCents": price.unit_amount,
        "currency": price.currency or "usd",
        "formattedPrice": _format_money(price.unit_amount, price.currency) + _recurring_suffix(price),
        "images": images,
        "priceType": price.type,
    }


@app.get("/api/products")
def api_products():
    try:
        listed = stripe.Product.list(active=True, limit=100, expand=["data.default_price"])
    except stripe.StripeError as exc:
        return _stripe_error_response(exc, 502)

    items: list[dict] = []
    for prod in listed.data:
        price: stripe.Price | None = None
        dp = prod.default_price
        if dp:
            if isinstance(dp, str):
                try:
                    price = stripe.Price.retrieve(dp)
                except stripe.StripeError:
                    price = None
            else:
                price = dp
        if not price:
            try:
                prices = stripe.Price.list(product=prod.id, active=True, limit=1)
            except stripe.StripeError:
                continue
            if not prices.data:
                continue
            price = prices.data[0]
        payload = _product_payload(prod, price)
        if payload:
            items.append(payload)

    return jsonify({"products": items})


def _checkout_base_url() -> str:
    return request.url_root.rstrip("/")


def _normalize_checkout_line_items(raw: list) -> list[dict] | None:
    line_items: list[dict] = []
    for item in raw:
        price_id = item.get("price") or item.get("priceId")
        qty = int(item.get("quantity") or 1)
        if not price_id or qty < 1 or qty > 99:
            continue
        line_items.append({"price": price_id, "quantity": qty})
    return line_items if line_items else None


@app.post("/api/create-checkout-session")
def create_checkout_session():
    data = request.get_json(silent=True) or {}
    raw = data.get("line_items") or data.get("items") or []
    if not raw:
        return jsonify({"error": "El carrito está vacío."}), 400

    line_items = _normalize_checkout_line_items(raw)
    if not line_items:
        return jsonify({"error": "No hay líneas de pedido válidas."}), 400

    modes: set[str] = set()
    try:
        for li in line_items:
            price = stripe.Price.retrieve(li["price"])
            if not price.active:
                return jsonify({"error": "Uno de los precios ya no está disponible."}), 400
            modes.add("subscription" if price.type == "recurring" else "payment")
    except stripe.StripeError as exc:
        return _stripe_error_response(exc, 400)

    if len(modes) > 1:
        return jsonify(
            {"error": "No puedes combinar suscripciones y pagos únicos en un solo checkout."}
        ), 400

    mode = modes.pop()
    cancel_url = f"{_checkout_base_url()}{_safe_cancel_path(data.get('cancel_path'))}"
    try:
        session = _new_checkout_session(mode, line_items, cancel_url)
    except stripe.StripeError as exc:
        return _stripe_error_response(exc, 502)

    return jsonify({"url": session.url})


@app.get("/api/checkout-session")
def get_checkout_session():
    sid = request.args.get("session_id")
    if not sid:
        return jsonify({"error": "Falta session_id."}), 400
    try:
        sess = stripe.checkout.Session.retrieve(
            sid,
            expand=["line_items"],
        )
    except stripe.StripeError as exc:
        return _stripe_error_response(exc, 400)

    return jsonify(
        {
            "status": sess.status,
            "payment_status": sess.payment_status,
            "customer_email": (
                sess.customer_details.email if sess.customer_details else None
            ),
            "amount_total": sess.amount_total,
            "currency": sess.currency,
            "mode": sess.mode,
        }
    )


@app.get("/")
def root():
    return send_from_directory(ROOT, "index.html")


@app.get("/<path:filename>")
def site_files(filename: str):
    if filename.startswith("api/"):
        abort(404)
    path = safe_join(str(ROOT), filename)
    if path is None or not os.path.isfile(path):
        abort(404)
    return send_from_directory(ROOT, filename)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=True)
