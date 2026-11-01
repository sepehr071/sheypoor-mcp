import pytest

pytestmark = [pytest.mark.live, pytest.mark.anyio]


async def test_live_search_and_ad(client):
    r = (
        await client.call_tool("sp_search", {"city": "tehran", "category": "car", "min_price": 100_000_000, "limit": 5})
    ).structured_content
    assert r["total"] > 100 and len(r["ads"]) == 5
    assert all(a["price"] is None or a["price"] >= 100_000_000 for a in r["ads"] if not a["promoted"]), r
    ad = (await client.call_tool("sp_ad", {"id": r["ads"][0]["id"]})).structured_content
    assert ad["title"] and ad["url"].startswith("https://www.sheypoor.com/v/")


async def test_live_sort_and_year_filter(client):
    r = (
        await client.call_tool(
            "sp_search",
            {
                "city": "tehran",
                "category": "car",
                "sort": "cheapest",
                "min_price": 50_000_000,
                "filters": {"mn68101": "1400"},
            },
        )
    ).structured_content
    prices = [a["price"] for a in r["ads"] if not a["promoted"] and a["price"]]
    assert prices == sorted(prices) and all(
        a["details"].get("سال تولید (چهار رقمی)", "1400") >= "1400" for a in r["ads"]
    )


async def test_live_price_summary(client):
    r = (
        await client.call_tool(
            "sp_price_summary", {"query": "206", "city": "tehran", "category": "car/peugeot", "pages": 1}
        )
    ).structured_content
    assert r["priced"] > 5 and r["min"] <= r["median"] <= r["max"]


async def test_live_lookups(client):
    c = (await client.call_tool("sp_cities", {"query": "مشهد"})).structured_content
    assert any(m["slug"] == "mashhad" for m in c["matches"])
    k = (await client.call_tool("sp_categories", {"query": "خودرو"})).structured_content
    assert any(x["slug"] == "car" for g in k["groups"] for x in g["categories"])
    f = (await client.call_tool("sp_filters", {"category": "car", "city": "tehran"})).structured_content
    assert f["subcategories"] and any(x.get("name") == "a69130" for x in f["filters"])


async def test_live_rent_filters_and_areas(client):
    f = (
        await client.call_tool("sp_filters", {"category": "house-apartment-for-rent", "city": "tehran", "area": "پونک"})
    ).structured_content
    assert any(x.get("range", [""])[0] == "mn68090" for x in f["filters"]) and f["neighbourhoods"][0]["id"] == "974"
    r = (
        await client.call_tool(
            "sp_search",
            {"city": "tehran", "category": "house-apartment-for-rent", "neighbourhoods": ["974"], "limit": 5},
        )
    ).structured_content
    assert r["matching"] > 20 and all("پونک" in a["location"] for a in r["ads"] if not a["promoted"])
    assert all(a["prices"] for a in r["ads"] if not a["promoted"])
    p = (
        await client.call_tool("sp_area_prices", {"city": "tehran", "category": "house-apartment-for-rent", "top": 2})
    ).structured_content
    assert len(p["areas"]) == 2 and all(a["median_deposit"] for a in p["areas"])


async def test_live_similar_shops_and_car_prices(client):
    ads = (
        await client.call_tool("sp_search", {"city": "tehran", "category": "car/peugeot", "limit": 3})
    ).structured_content
    ad = next(a for a in ads["ads"] if not a["promoted"] and a["price"])
    s = (await client.call_tool("sp_similar", {"id": ad["id"]})).structured_content
    assert s["category"].startswith("car") and all(a["id"] != ad["id"] for a in s["ads"])
    shops = (
        await client.call_tool("sp_shops", {"city": "tehran", "category": "real-estate", "limit": 12})
    ).structured_content
    assert shops["total"] > 50
    shop = (await client.call_tool("sp_shop", {"slug": shops["shops"][0]["slug"], "limit": 2})).structured_content
    assert shop["name"] == shops["shops"][0]["name"]
    cars = (await client.call_tool("sp_car_prices", {"group": "saipa-group"})).structured_content
    assert cars["models"] > 3 and cars["cars"][0]["years"][0]["market"] > 10**8
