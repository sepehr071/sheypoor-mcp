"""Sheypoor tools: ad search, ad details, price summary, categories, cities and per-category filters."""

from __future__ import annotations

import html
import re
import statistics
from typing import Annotated, Any, Literal

from pydantic import Field

from .http import ApiError, fetch
from .registry import tool

CATEGORY_PATTERN = r"^[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+){0,2}$"  # car, car/peugeot, car/peugeot/206

City = Annotated[
    str,
    Field(
        description="City or province slug from sp_cities, e.g. tehran. 'iran' = all Iran.",
        pattern=r"^[A-Za-z0-9-]{2,60}$",
    ),
]
Category = Annotated[
    str | None,
    Field(
        description="Slug from sp_categories (car, mobile-phone) or sp_filters path (car/peugeot/206). Omit for all.",
        pattern=CATEGORY_PATTERN,
    ),
]
Query = Annotated[str | None, Field(description="Persian words, e.g. پراید.", max_length=200)]
MinPrice = Annotated[int | None, Field(description="Lowest price, Toman.", ge=0, le=10**13)]
MaxPrice = Annotated[int | None, Field(description="Highest price, Toman.", ge=0, le=10**13)]
Areas = Annotated[
    list[Annotated[str, Field(pattern=r"^\d{1,7}$")]] | None,
    Field(
        description="Neighbourhood ids from sp_filters(area=...), e.g. ['888']. City-level only.",
        max_length=10,
    ),
]
Filters = Annotated[
    dict[str, str] | None,
    Field(
        description="Filters from sp_filters as {name: value}: a<id> option id, mn<id>/mx<id> range ends (mn68101: '1400' = year from)."
    ),
]

# Sort codes of the site's sort menu (sp_filters lists which ones a category offers).
SORT = {
    "newest": "n",
    "cheapest": "pa",
    "priciest": "pd",
    "lowest_km": "kma",
    "highest_km": "kmd",
    "oldest_year": "opy",
    "newest_year": "npy",
}
SortName = Literal["newest", "cheapest", "priciest", "lowest_km", "highest_km", "oldest_year", "newest_year"]
FILTER_KEY = re.compile(r"^(a|mn|mx)\d{1,7}$")
MAX_ITEMS = 40
JUNK_PRICE = 10_000  # sellers type 1,000 Toman as a placeholder; the price summary ignores prices below this

_FA = str.maketrans("يكۀ", "یکه")  # Arabic yeh, kaf, heh with hamza -> Persian


def _norm(s: str) -> str:
    """Fold Arabic letter variants and the half-space so Persian name matching is forgiving."""
    return s.translate(_FA).replace("\u200c", " ").strip().lower()


def _price(p: Any) -> tuple[int | None, str | None]:
    """(Toman, label) from an ad's price list; the amount is None when the price is negotiable or hidden."""
    if not p:
        return None, None
    first = p[0] if isinstance(p, list) else p
    amount = str(first.get("amount") or "")
    digits = re.sub(r"\D", "", amount)
    return (int(digits) if digits else None), (amount if not digits else None)


def _prices(p: Any) -> dict[str, Any]:
    """Labelled prices of rentals ({'رهن': deposit, 'اجاره': rent}); empty for ordinary single-price ads."""
    out = {}
    for x in p if isinstance(p, list) else []:
        n, text = _price([x])
        if x.get("label") and x["label"] != "قیمت":  # sale ads label a negotiable price "قیمت"
            out[x["label"]] = n if n is not None else text
    return out


def _text(s: str | None) -> str:
    """Ad description without the markup the site appends (the masked phone span, <br>)."""
    s = re.sub(r"<span[^>]*>.*?</span>", "", s or "", flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", "\n", s)).strip()


def _item(x: dict[str, Any], kind: str) -> dict[str, Any]:
    a = x.get("attributes", {})
    price, text = _price(a.get("price"))
    cats = [c.get("name") for c in a.get("categories", []) if c.get("name")]
    attrs = {}
    for f in x.get("fullAttributes", []) or []:
        if f.get("key") and f.get("value") not in (None, "") and (len(attrs) < 8 or f["key"] == "متراژ"):
            attrs[f["key"]] = f["value"]
    return {
        "id": x.get("id"),
        "title": a.get("title"),
        "price": price,
        "price_text": text,
        "prices": _prices(a.get("price")),
        "location": a.get("location"),
        "posted": a.get("timePassedLabel"),
        "category": " > ".join(cats[1:] or cats),
        "details": attrs,
        "photos": a.get("imageCount"),
        "tags": [t.get("label") for t in a.get("paidTags", []) if t.get("label")],
        "promoted": kind in ("special", "vip", "paidEngagement"),
        "nearby": kind == "nearby" or None,
        "url": f"https://www.sheypoor.com/v/{x['id']}",  # the card's own slug drops Persian-only letters; /v/<id> redirects
    }


def _flatten(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ad cards of one search page: the normal list plus showcase blocks; banners and widgets are skipped. Deduplicated."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for block in data:
        t = block.get("type")
        cards = (
            block.get("items")
            if t in ("special", "vip")
            else [block]
            if t in ("normal", "paidEngagement", "nearby")
            else []
        )
        for c in cards or []:
            if c.get("id") and c["id"] not in seen and "attributes" in c:
                seen.add(c["id"])
                out.append(_item(c, c.get("type", t)))
    return out


def _params(
    query: str | None,
    min_price: int | None,
    max_price: int | None,
    sort: str | None,
    filters: dict[str, str] | None,
    page: int = 1,
    areas: list[str] | None = None,
) -> dict[str, Any]:
    if min_price is not None and max_price is not None and min_price > max_price:
        raise ApiError("min_price is above max_price.")
    for k in filters or {}:
        if not FILTER_KEY.match(k):
            raise ApiError(f"Unknown filter name {k!r}: use names from sp_filters (a<id>, mn<id>, mx<id>).")
    p: dict[str, Any] = {
        "q": (query or "").strip() or None,
        "mnp": min_price,
        "mxp": max_price,
        "o": SORT.get(sort or ""),
    }
    p.update(filters or {})
    if areas:
        p["nh[]"] = list(areas)
    if page > 1:
        p["p"] = page
    return p


def _path(city: str, category: str | None) -> str:
    return f"search/{city}/{category}" if category else f"search/{city}"


async def _page(city, category, params) -> dict[str, Any]:
    try:
        return await fetch(_path(city, category), params)
    except ApiError as e:
        if e.status == 404:
            raise ApiError(
                f"No such city or category ({city}/{category or ''}). Look the slugs up with sp_cities and sp_categories.",
                404,
            ) from e
        raise


@tool("Search ads")
async def sp_search(
    query: Query = None,
    city: City = "iran",
    category: Category = None,
    min_price: MinPrice = None,
    max_price: MaxPrice = None,
    sort: Annotated[
        SortName,
        Field(description="Cars also: km and year orders."),
    ] = "newest",
    filters: Filters = None,
    neighbourhoods: Areas = None,
    page: Annotated[int, Field(description="24 ads per page.", ge=1, le=50)] = 1,
    limit: Annotated[int, Field(description="Max ads.", ge=1, le=MAX_ITEMS)] = 20,
) -> dict[str, Any]:
    """Search Sheypoor classified ads (cars, real estate, goods, jobs, services) by words, city, category and price.

    Call sp_ad with an ad `id` for details; sp_filters first for attribute filters. `price` is Toman (null =
    negotiable, see `price_text`); 1,000 is a placeholder, so set min_price when sorting by price. Rentals carry
    `prices` ({رهن deposit, اجاره rent}). `nearby` and `promoted` ads may not match your filters. `matching` is
    the real match count; `total` includes padding. For a median use sp_price_summary.
    """
    p = _params(query, min_price, max_price, sort if sort != "newest" else None, filters, page, neighbourhoods)
    d = await _page(city, category, p)
    ads = _flatten(d.get("data", []))[:limit]
    m = d.get("meta", {})
    return {
        "matching": m.get("normal_count"),
        "total": m.get("total"),
        "page": page,
        "per_page": m.get("items_per_page"),
        "ads": ads,
    }


def _where(crumbs: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """(city slug, deepest category path) of an ad from its breadcrumb links, e.g. ('tehran', 'car/hyundai/creta')."""
    city = next((b["url"].rsplit("/", 1)[-1] for b in crumbs if b.get("type") == "city" and b.get("url")), None)
    cats = [b["url"] for b in crumbs if b.get("type") == "category" and b.get("url")]
    path = cats[-1].split(f"/s/{city}/", 1)[1] if cats and city and f"/s/{city}/" in cats[-1] else None
    return city, path


@tool("Ad details")
async def sp_ad(
    id: Annotated[str, Field(description="Ad id from sp_search.", pattern=r"^\d{4,12}$")],
    photos: Annotated[int, Field(description="Photo URLs to include.", ge=0, le=10)] = 3,
) -> dict[str, Any]:
    """Get one Sheypoor ad in full: description, price, location, seller, every attribute and photo links.

    `city_slug` and `category_slug` plug into sp_search; sp_similar finds comparable ads. Rentals have `prices`
    (رهن deposit, اجاره rent). The seller's phone is not available; open `url` to contact the seller.
    """
    try:
        d = (await fetch(f"listings/{id}"))["data"]
    except ApiError as e:
        if e.status == 404:
            raise ApiError(f"Ad {id} not found: it may have expired or been removed.", 404) from e
        raise
    a = d["attributes"]
    price, text = _price(a.get("price"))
    s = a.get("seller") or {}
    rates = s.get("rates") or {}
    crumbs = [b.get("title") for b in a.get("breadcrumbs", []) if b.get("title")]
    city, cat_path = _where(a.get("breadcrumbs", []))
    agent = a.get("consultant") or {}
    details = {
        f["key"]: f["value"] for f in a.get("attributes", []) if f.get("key") and f.get("value") not in (None, "")
    }
    prices = _prices(a.get("price")) or {
        k: n for k in ("رهن", "اجاره") if (n := _price([{"amount": details.get(k)}])[0])
    }
    insp = a.get("carInspection") or {}
    return {
        "id": d.get("id"),
        "title": a.get("title"),
        "price": price,
        "price_text": text,
        "prices": prices,
        "market_range": a.get("priceRange"),
        "location": a.get("location"),
        "category": " > ".join(crumbs),
        "city_slug": city,
        "category_slug": cat_path,
        "posted": a.get("timePassedLabel"),
        "added_at": a.get("addedAt"),
        "description": _text(a.get("description"))[:3000],
        "details": {
            f["key"]: f["value"] for f in a.get("attributes", []) if f.get("key") and f.get("value") not in (None, "")
        },
        "seller": {
            "name": s.get("name"),
            "shop": a.get("isShopProfile"),
            "member_since": s.get("registeredAt"),
            "rating": rates.get("score"),
            "ratings": rates.get("count"),
            "ads": s.get("listingCount"),
        },
        "agent": {"name": agent.get("name"), "agency": agent.get("shopName"), "ads": agent.get("countListings")}
        if agent
        else None,
        "inspection": insp.get("summary"),
        "videos": a.get("videoCount"),
        "photo_count": a.get("imageCount"),
        "photos": [i["source"].get("mobile") for i in a.get("images", [])[:photos] if i.get("source")],
        "url": a.get("url"),
    }


@tool("Price summary for a search")
async def sp_price_summary(
    query: Query = None,
    city: City = "iran",
    category: Category = None,
    min_price: MinPrice = None,
    max_price: MaxPrice = None,
    filters: Filters = None,
    neighbourhoods: Areas = None,
    pages: Annotated[int, Field(description="Result pages (24 ads each) to sample.", ge=1, le=5)] = 3,
) -> dict[str, Any]:
    """Summarise asking prices of the newest ads for a search: count, min, median, quartiles, max, plus cheapest and priciest ads.

    Use it for "what does X cost on Sheypoor": asking prices of the newest ads, not sold prices. Ads without a
    numeric price and prices under 10,000 Toman are left out. Narrow the query (year, brand, city) for a useful
    median. Values are in Toman.
    """
    floor = max(min_price or 0, JUNK_PRICE)
    base = _params(query, floor, max_price, None, filters, areas=neighbourhoods)
    ads: dict[str, dict[str, Any]] = {}
    total = None
    for n in range(1, pages + 1):
        d = await _page(city, category, {**base, **({"p": n} if n > 1 else {})})
        total = d.get("meta", {}).get("total", total)
        got = _flatten(d.get("data", []))
        if not got:
            break
        for a in got:
            ads.setdefault(a["id"], a)
    priced = sorted((a for a in ads.values() if a["price"] and a["price"] >= floor), key=lambda a: a["price"])
    if not priced:
        return {"total_matching": total, "sampled": len(ads), "priced": 0, "note": "No ads with a numeric price."}
    vals = [a["price"] for a in priced]
    q = statistics.quantiles(vals, n=4) if len(vals) > 1 else [vals[0]] * 3
    brief = lambda a: {k: a[k] for k in ("id", "title", "price", "location", "url")}  # noqa: E731
    return {
        "total_matching": total,
        "sampled": len(ads),
        "priced": len(vals),
        "min": vals[0],
        "p25": round(q[0]),
        "median": round(statistics.median(vals)),
        "p75": round(q[2]),
        "max": vals[-1],
        "cheapest": [brief(a) for a in priced[:3]],
        "priciest": [brief(a) for a in priced[-3:][::-1]],
        "unit": "Toman",
    }


@tool("Categories")
async def sp_categories(
    query: Annotated[
        str | None,
        Field(description="Part of a category name or slug, e.g. خودرو.", max_length=60),
    ] = None,
) -> dict[str, Any]:
    """List Sheypoor's categories with the slug sp_search and sp_filters take, grouped under 11 top-level groups.

    Car brands and models are deeper: sp_filters(category="car") lists them (car/peugeot). Use a sub-category
    slug alone (car, not vehicles/car).
    """
    d = await fetch("categories/compact")
    by_id = {x["id"]: x for x in d.get("included", []) + d["data"]}
    q = _norm(query) if query else None
    hit = lambda c: not q or q in _norm(c["attributes"]["name"]) or q in c["attributes"]["slug"].lower()  # noqa: E731
    groups = []
    for g in d["data"]:
        kids = [
            by_id[k["id"]]
            for k in (g.get("relationships") or {}).get("children", {}).get("data", [])
            if k["id"] in by_id
        ]
        kept = [c for c in kids if hit(c)] if not hit(g) else kids
        if kept or hit(g):
            groups.append(
                {
                    "name": g["attributes"]["name"],
                    "slug": g["attributes"]["slug"].strip(),
                    "categories": [
                        {"name": c["attributes"]["name"], "slug": c["attributes"]["slug"].strip()} for c in kept
                    ],
                }
            )
    return {"groups": groups}


@tool("Cities and provinces")
async def sp_cities(
    query: Annotated[
        str | None,
        Field(
            description="Part of a city or province name or slug, e.g. مشهد. Omit to list provinces.",
            max_length=60,
        ),
    ] = None,
    limit: Annotated[int, Field(description="Max cities.", ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """Find the city or province slug that sp_search, sp_price_summary and sp_filters take.

    A province slug also works as `city` and covers all its cities. Without `query`, lists the 31 provinces.
    """
    d = (await fetch("general/locations"))["data"]["list"]
    if not query:
        return {"provinces": [{"name": p["name"], "slug": p["slug"], "cities": len(p["cities"])} for p in d]}
    q = _norm(query)
    out = []
    for p in d:
        if q in _norm(p["name"]) or q in p["slug"]:
            out.append({"type": "province", "name": p["name"], "slug": p["slug"]})
        for c in p["cities"]:
            if q in _norm(c["name"]) or q in c["slug"]:
                out.append(
                    {
                        "type": "city",
                        "name": c["name"],
                        "slug": c["slug"],
                        "province": p["name"],
                        "province_slug": p["slug"],
                    }
                )
    return {"matches": out[:limit], "more": max(0, len(out) - limit)}


@tool("Filters of a category")
async def sp_filters(
    category: Annotated[
        str,
        Field(
            description="Slug from sp_categories, e.g. car, house-apartment-for-rent. A brand path (car/peugeot) lists its models.",
            pattern=CATEGORY_PATTERN,
        ),
    ],
    city: City = "iran",
    area: Annotated[
        str | None,
        Field(
            description="Part of a neighbourhood name, e.g. پونک. Needs a city (not 'iran').",
            max_length=60,
        ),
    ] = None,
) -> dict[str, Any]:
    """List the filters, sort orders, sub-categories (car brands, models) and neighbourhoods Sheypoor offers in one category.

    Call before using `filters` in sp_search or sp_price_summary. Choice filters have a `name` and `options`
    {id, label}: pass {name: id}. Range filters list min/max names: pass a plain number. Yes/no filters have
    `value` '1'. `subcategories` slugs go in `category`; `neighbourhoods` ids go in `neighbourhoods`; `area`
    searches the full list by name. Prices use min_price/max_price.
    """
    d = await _filters(city, category)
    meta = d.get("meta", {})
    inc = {(x["type"], x["id"]): x for x in d.get("included", [])}
    labels = {
        x["id"]: x["attributes"].get("label") for x in d.get("included", []) if x["type"] == "formComponentOptions"
    }
    filters = []
    for g in d.get("data", []):
        for ref in (g.get("relationships") or {}).get("components", {}).get("data", []):
            c = inc.get((ref["type"], ref["id"]))
            if not c:
                continue
            a = c["attributes"]
            kind = ref["type"].removeprefix("formComponent")
            if kind == "BrandAndModels" or a.get("name") == "nh":
                continue
            if kind.startswith("Range"):
                if a["min"]["name"] != "mnp":  # price: min_price / max_price
                    filters.append(
                        {
                            "label": a.get("label"),
                            "range": [a["min"]["name"], a["max"]["name"]],
                            "unit": a.get("unitCaption"),
                        }
                    )
                continue
            if kind == "Boolean":
                filters.append({"label": a.get("label"), "name": a.get("name"), "value": "1"})
                continue
            opts = [o["id"] for o in (c.get("relationships") or {}).get("formComponentOptions", {}).get("data", [])]
            filters.append(
                {
                    "label": a.get("label"),
                    "name": a.get("name"),
                    "options": [{"id": i, "label": labels.get(i)} for i in opts[:40]] or None,
                }
            )
    links = meta.get("links", [])
    q = _norm(area) if area else None
    areas = [x for x in links if x.get("type") == "neighbourhood" and (not q or q in _norm(x.get("name", "")))]
    return {
        "sort": [
            {"title": s["title"], "code": s["value"]} for s in meta.get("sorting_orders", []) if s["value"] != "am"
        ],
        "subcategories": [
            {
                "name": s["name"],
                "slug": s["slug"] if s["type"] == "subCategory" else f"{category}/{s['slug']}",
                "ads": s.get("count"),
            }
            for s in links
            if s.get("type") in ("subCategory", "car_model") and s.get("slug")
        ][:60],
        "neighbourhoods": [{"id": x["id"], "name": x["name"], "ads": x.get("count")} for x in areas][
            : 60 if not q else 20
        ],
        "filters": filters,
    }


async def _filters(city: str, category: str) -> dict[str, Any]:
    try:
        return await fetch(f"search/filters/{city}/{category}")
    except ApiError as e:
        if e.status == 404:
            raise ApiError(
                f"No such city or category ({city}/{category}). Look the slugs up with sp_cities and sp_categories.",
                404,
            ) from e
        raise
