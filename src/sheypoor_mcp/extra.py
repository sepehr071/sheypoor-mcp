"""Sheypoor tools built on more of the site: similar ads, internet shops, daily car prices, per-neighbourhood prices."""

from __future__ import annotations

import asyncio
import json
import re
import statistics
from typing import Annotated, Any, Literal

from pydantic import Field

from .ads import CATEGORY_PATTERN, JUNK_PRICE, City, _filters, _flatten, _norm, _page, _params, _price, _prices, _where
from .http import ApiError, fetch
from .registry import tool

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٫،", "0123456789.,")
_PUSH = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')
SLUG = r"^[A-Za-z0-9_-]{1,80}$"

CarGroup = Literal[
    "domestic-group", "iran-khodro-group", "saipa-group", "foreign-group", "modiran-group", "kerman-group"
]


def _brief(a: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "title", "price", "price_text", "prices", "location", "posted", "details", "photos", "url")
    return {k: a[k] for k in keys if a.get(k) not in (None, "", {}, [])}


@tool("Similar ads")
async def sp_similar(
    id: Annotated[str, Field(description="Ad id from sp_search.", pattern=r"^\d{4,12}$")],
    price_range_pct: Annotated[
        int, Field(description="Keep ads within this percent of the ad's price.", ge=5, le=100)
    ] = 30,
    limit: Annotated[int, Field(description="Max ads.", ge=1, le=24)] = 12,
) -> dict[str, Any]:
    """Find ads comparable to one ad: same city and deepest category (car model...), price within a percent band.

    Use it to judge whether an ad is cheap or dear. With fewer than 3 matches it widens to the parent category
    (shown in `category`). Rentals and ads without a numeric price match by category only. Newest first; the ad
    itself and paid showcase ads are left out.
    """
    try:
        a = (await fetch(f"listings/{id}"))["data"]["attributes"]
    except ApiError as e:
        if e.status == 404:
            raise ApiError(f"Ad {id} not found: it may have expired or been removed.", 404) from e
        raise
    city, path = _where(a.get("breadcrumbs", []))
    if not city or not path:
        raise ApiError("This ad has no category/city links, so no similar ads can be searched.")
    price, _ = _price(a.get("price"))
    labelled = isinstance(a.get("price"), list) and len(a["price"]) > 1
    lo = hi = None
    if price and not labelled and price >= JUNK_PRICE:
        lo, hi = int(price * (1 - price_range_pct / 100)), int(price * (1 + price_range_pct / 100))
    ads: list[dict[str, Any]] = []
    total = None
    while True:
        d = await _page(city, path, _params(None, lo, hi, None, None))
        total = d.get("meta", {}).get("normal_count")
        ads = [x for x in _flatten(d.get("data", [])) if x["id"] != id and not x["promoted"] and not x["nearby"]]
        if len(ads) >= 3 or "/" not in path:
            break
        path = path.rsplit("/", 1)[0]
    return {
        "based_on": {"id": id, "title": a.get("title"), "price": price, "location": a.get("location")},
        "city": city,
        "category": path,
        "price_band": [lo, hi] if lo else None,
        "matching": total,
        "ads": [_brief(x) for x in ads[:limit]],
    }


@tool("Internet shops")
async def sp_shops(
    city: City = "iran",
    category: Annotated[
        str | None,
        Field(
            description="Top-level group slug from sp_categories, e.g. real-estate, vehicles. Omit for all.",
            pattern=SLUG,
        ),
    ] = None,
    page: Annotated[int, Field(description="Page.", ge=1, le=200)] = 1,
    limit: Annotated[int, Field(description="Shops per page.", ge=1, le=24)] = 12,
) -> dict[str, Any]:
    """List Sheypoor's internet shops and agencies (real-estate offices, car dealers...) in a city and group.

    Call sp_shop with a shop `slug` for details. `category` is a top-level group, not a sub-category such as
    car. Shops with no ads are left out, so a page may hold fewer than `limit`.
    """
    path = f"shops/{city}/{category}" if category else f"shops/{city}"
    try:
        d = await fetch(path, {"page[number]": page if page > 1 else None, "page[size]": limit})
    except ApiError as e:
        if e.status == 404:
            raise ApiError(
                f"No such city or group ({city}/{category or ''}). Use sp_cities and sp_categories.", 404
            ) from e
        raise
    shops = []
    for x in d.get("data", []):
        a = x["attributes"]
        if not a.get("listingsCount"):  # expired shop: listed, but shops/slug/<slug> and its page answer 404
            continue
        shops.append(
            {
                "slug": a.get("slug"),
                "name": a.get("title"),
                "group": a.get("categoryName"),
                "location": a.get("location"),
                "ads": a.get("listingsCount"),
                "about": a.get("tagLine") or a.get("description"),
                "url": a.get("detailsUrl"),
            }
        )
    return {"total": d.get("meta", {}).get("total_items"), "page": page, "shops": shops}


@tool("Shop details")
async def sp_shop(
    slug: Annotated[str, Field(description="Shop slug from sp_shops.", pattern=SLUG)],
    limit: Annotated[int, Field(description="Max of the shop's ads.", ge=0, le=24)] = 12,
) -> dict[str, Any]:
    """Get one internet shop or agency: name, hours, group, its agents and its current ads.

    Phone numbers are left out. Each ad has an `id` for sp_ad.
    """
    try:
        d = (await fetch(f"shops/slug/{slug}"))["data"]["attributes"]
    except ApiError as e:
        if e.status == 404:
            raise ApiError(f"Shop {slug} not found. Look the slug up with sp_shops.", 404) from e
        raise
    ads = []
    total = None
    if limit:
        ls = await fetch(f"shops/slug/{slug}/listings", {"page[size]": 24})
        total = ls.get("meta", {}).get("total_items")
        for x in ls.get("data", [])[:limit]:
            a = x["attributes"]
            price, text = _price(a.get("price"))
            ads.append(
                {
                    "id": x["id"],
                    "title": a.get("title"),
                    "price": price,
                    "price_text": text,
                    "prices": _prices(a.get("price")),
                    "location": a.get("location"),
                    "posted": a.get("timePassedLabel"),
                    "photos": a.get("imageCount"),
                    "url": a.get("url"),
                }
            )
    return {
        "slug": slug,
        "name": d.get("title"),
        "about": d.get("tagLine"),
        "hours": d.get("workingTime"),
        "member_since": d.get("registeredAt"),
        "places": [b.get("title") for b in d.get("breadcrumbs", [])],
        "agents": [
            {"name": c.get("name"), "since": c.get("registeredFrom"), "ads": c.get("listingsCount")}
            for c in d.get("consultants", [])
        ],
        "ads_total": total,
        "ads": ads,
    }


def _num(s: str | None) -> int | None:
    digits = re.sub(r"\D", "", (s or "").translate(_DIGITS))
    return int(digits) or None if digits else None


def _price_list(html: str) -> dict[str, Any]:
    """The daily price list that the car-price page embeds for its client app (Next.js flight data)."""
    flight = "".join(json.loads(p) for p in _PUSH.findall(html))
    k = flight.find('"queryKey":["LOAD_PRICE_LIST"')
    s = flight.rfind('"data":{"success"', 0, k)
    if k < 0 or s < 0:
        raise ApiError("The car-price page has no price list (the site layout may have changed).")
    return json.JSONDecoder().raw_decode(flight, s + 7)[0]


@tool("Daily car prices")
async def sp_car_prices(
    query: Annotated[
        str | None,
        Field(description="Brand or model words, Persian or English, e.g. پراید, 206.", max_length=60),
    ] = None,
    group: Annotated[
        CarGroup | None,
        Field(description="Maker group. Omit for all."),
    ] = None,
    year: Annotated[str | None, Field(description="Model year, e.g. 1404.", pattern=r"^\d{4}$")] = None,
    limit: Annotated[int, Field(description="Max models.", ge=1, le=60)] = 20,
) -> dict[str, Any]:
    """Get Sheypoor's daily car price list (قیمت روز خودرو): market price and factory price per model and year, with the day's change.

    Use it to check a car's price against the market average (quotes, not ads). `market` is the open-market
    price, `factory` the maker's list price. Toman; `change_pct` is today's market move. For ad prices use
    sp_price_summary.
    """
    html = await fetch(f"car-price/{group}" if group else "car-price", page=True)
    q = _norm(query) if query else None
    out = []
    for brand in _price_list(html).get("results", []):
        for m in brand.get("children", []):
            names = _norm(f"{brand.get('title')} {brand.get('en_title')} {m.get('title')} {m.get('en_title')}")
            if q and not all(w in names for w in q.split()):
                continue
            years = []
            for g in m.get("group_keys", []):
                if year and g.get("key") != year:
                    continue
                years.append(
                    {
                        "year": g.get("key"),
                        "market": _num(g.get("avg_price_market")),
                        "factory": _num(g.get("avg_price_company")),
                        "change": g.get("change_market", "").translate(_DIGITS).replace(",", "") or None,
                        "change_pct": g.get("change_percent_market_raw"),
                        "updated": g.get("last_update_at_market", "").translate(_DIGITS) or None,
                    }
                )
            if years:
                out.append({"brand": brand.get("title"), "model": m.get("title"), "years": years})
    return {"unit": "Toman", "models": len(out), "cars": out[:limit]}


def _median(xs: list[int]) -> int | None:
    return round(statistics.median(xs)) if xs else None


@tool("Prices per neighbourhood")
async def sp_area_prices(
    city: Annotated[str, Field(description="City slug, e.g. tehran (not 'iran').", pattern=r"^[A-Za-z0-9-]{2,60}$")],
    category: Annotated[
        str,
        Field(
            description="Category slug, e.g. house-apartment-for-rent, car.",
            pattern=CATEGORY_PATTERN,
        ),
    ],
    neighbourhoods: Annotated[
        list[Annotated[str, Field(pattern=r"^\d{1,7}$")]] | None,
        Field(description="Neighbourhood ids from sp_filters(area=...). Omit for the busiest.", max_length=8),
    ] = None,
    top: Annotated[int, Field(description="Busiest neighbourhoods when none are given.", ge=1, le=8)] = 5,
) -> dict[str, Any]:
    """Compare asking prices between neighbourhoods of a city: median price, price per square metre, rent and deposit.

    Use it for "where is renting cheapest in Tehran". It samples each area's newest 24 ads, so medians are
    rough. Sale: `median_price`, `median_per_m2`; rentals: `median_deposit`, `median_rent`. Toman. Pick ids with
    sp_filters(category, city, area=...).
    """
    meta = (await _filters(city, category)).get("meta", {})
    known = {x["id"]: x for x in meta.get("links", []) if x.get("type") == "neighbourhood"}
    if not known:
        raise ApiError(
            f"{category} in {city} has no neighbourhood list. Use a city (not a province) and a real-estate or similar category."
        )
    ids = neighbourhoods or [x["id"] for x in sorted(known.values(), key=lambda x: -x.get("count", 0))[:top]]

    async def one(i: str) -> dict[str, Any]:
        d = await _page(city, category, _params(None, None, None, None, None, areas=[i]))
        ads = [a for a in _flatten(d.get("data", [])) if not a["promoted"] and not a["nearby"]]
        prices = [a["price"] for a in ads if a["price"] and a["price"] >= JUNK_PRICE]
        per_m2 = [
            round(a["price"] / m)
            for a in ads
            if a["price"] and (m := _num(str(a["details"].get("متراژ", "")))) and m >= 10
        ]
        dep = [v for a in ads if isinstance(v := a["prices"].get("رهن"), int) and v > 0]
        rent = [v for a in ads if isinstance(v := a["prices"].get("اجاره"), int) and v > 0]
        x = known.get(i, {})
        r = {
            "id": i,
            "name": x.get("name"),
            "ads": d.get("meta", {}).get("normal_count"),
            "sampled": len(ads),
        }
        if dep or rent:
            r |= {"median_deposit": _median(dep), "median_rent": _median(rent)}
        else:
            r |= {"median_price": _median(prices), "median_per_m2": _median(per_m2)}
        return r

    return {
        "city": city,
        "category": category,
        "unit": "Toman",
        "areas": list(await asyncio.gather(*(one(i) for i in ids))),
    }
