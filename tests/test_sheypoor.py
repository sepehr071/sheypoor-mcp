import httpx
import pytest
from conftest import FIXTURES, fixture

from sheypoor_mcp import http

pytestmark = pytest.mark.anyio

SEARCH = "/api/v10.0.0/search/tehran/car"
TOOLS = {
    "sp_search",
    "sp_ad",
    "sp_price_summary",
    "sp_categories",
    "sp_cities",
    "sp_filters",
    "sp_similar",
    "sp_shops",
    "sp_shop",
    "sp_car_prices",
    "sp_area_prices",
}
RENT = "/api/v10.0.0/search/tehran/house-apartment-for-rent"


async def test_all_tools_are_read_only_and_described(client):
    tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == TOOLS
    for t in tools:
        assert t.annotations.read_only_hint is True, t.name
        assert t.title and t.description.splitlines()[0].endswith("."), t.name
        assert "title" not in str(t.input_schema.get("properties", {}).values()), t.name


async def test_search_flattens_cards_and_sends_filters(client, api):
    api[SEARCH] = fixture("search.json")
    r = (
        await client.call_tool(
            "sp_search",
            {
                "query": "پراید",
                "city": "tehran",
                "category": "car",
                "min_price": 1000,
                "max_price": 5000,
                "sort": "cheapest",
                "filters": {"mn68101": "1400"},
                "page": 2,
            },
        )
    ).structured_content
    q = dict(api.calls[0].url.params)
    assert q == {"q": "پراید", "mnp": "1000", "mxp": "5000", "o": "pa", "mn68101": "1400", "p": "2"}
    assert r["total"] == 5294 and r["page"] == 2
    ids = [a["id"] for a in r["ads"]]
    assert len(ids) == len(set(ids)) > 4
    first = r["ads"][0]
    assert first["promoted"] is True and first["price"] == 650000000
    assert first["category"] == "خودرو > ساینا" and first["details"]["رنگ"]
    assert not any("telephone" in a or "phone" in a for a in r["ads"])
    assert any(a["promoted"] is False for a in r["ads"])


async def test_search_unknown_filter_name_and_bad_slug(client, api):
    r = await client.call_tool("sp_search", {"filters": {"evil": "1"}})
    assert r.is_error and "sp_filters" in r.content[0].text
    api[SEARCH] = lambda request: httpx.Response(404, json={"detail": "slugs does not matched"})
    r = await client.call_tool("sp_search", {"city": "tehran", "category": "car"})
    assert r.is_error and "sp_cities" in r.content[0].text


async def test_search_marks_nearby_ads(client, api):
    body = fixture("search.json")
    body["data"] = [
        {"type": "separator", "title": "x"},
        {"type": "nearby", "id": "9", "attributes": {"title": "n", "price": [{"amount": "200,000,000"}]}},
    ]
    api[SEARCH] = body
    ads = (await client.call_tool("sp_search", {"city": "tehran", "category": "car"})).structured_content["ads"]
    assert [(a["id"], a["nearby"], a["promoted"]) for a in ads] == [("9", True, False)]


async def test_search_negotiable_price_is_text(client, api):
    body = fixture("search.json")
    body["data"] = [
        {
            "type": "normal",
            "id": "1",
            "attributes": {"title": "x", "price": [{"label": "قیمت", "amount": "توافقی", "currency": ""}]},
        }
    ]
    api[SEARCH] = body
    ad = (await client.call_tool("sp_search", {"city": "tehran", "category": "car"})).structured_content["ads"][0]
    assert "price" not in ad and ad["price_text"] == "توافقی"


async def test_ad_details_drop_phone_and_html(client, api):
    api["/api/v10.0.0/listings/468673507"] = fixture("listing.json")
    r = (await client.call_tool("sp_ad", {"id": "468673507", "photos": 1})).structured_content
    assert r["price"] == 8960000000 and r["seller"]["rating"] == 4.25
    assert r["details"]["رنگ"] == "سفید" and len(r["photos"]) == 1
    assert "<" not in r["description"] and "XXX" not in r["description"]
    assert "phone" not in r and "0919" not in str(r)
    api.clear()
    r = await client.call_tool("sp_ad", {"id": "1234567"})
    assert r.is_error and "not found" in r.content[0].text


async def test_price_summary_stats(client, api):
    def page(request):
        body = fixture("search.json")
        prices = [100_000_000, 200_000_000, 300_000_000, 400_000_000, 1_000]
        body["data"] = [
            {
                "type": "normal",
                "id": str(i),
                "attributes": {"title": f"t{i}", "price": [{"amount": f"{p:,}", "currency": "تومان"}]},
            }
            for i, p in enumerate(prices, 1)
        ]
        return httpx.Response(200, json=body)

    api[SEARCH] = page
    r = (
        await client.call_tool("sp_price_summary", {"city": "tehran", "category": "car", "pages": 1})
    ).structured_content
    assert api.calls[0].url.params["mnp"] == "10000"  # placeholder prices are filtered server-side
    assert r["priced"] == 4 and r["min"] == 100_000_000 and r["max"] == 400_000_000
    assert r["median"] == 250_000_000 and r["priciest"][0]["id"] == "4"


async def test_categories_and_cities_matching(client, api):
    api["/api/v10.0.0/categories/compact"] = fixture("categories.json")
    r = (await client.call_tool("sp_categories", {"query": "خودرو"})).structured_content
    slugs = [c["slug"] for g in r["groups"] for c in g["categories"]]
    assert "car" in slugs
    api["/api/v10.0.0/general/locations"] = fixture("locations.json")
    r = (await client.call_tool("sp_cities", {"query": "مشهد"})).structured_content
    assert r["matches"][0]["slug"] == "mashhad" and r["matches"][0]["province_slug"] == "razavi-khorasan"
    assert (await client.call_tool("sp_cities", {})).structured_content["provinces"]


async def test_filters_lists_options_ranges_and_sorts(client, api):
    api["/api/v10.0.0/search/filters/tehran/car"] = fixture("filters.json")
    r = (await client.call_tool("sp_filters", {"category": "car", "city": "tehran"})).structured_content
    assert {"title": "ارزان\u200cترین", "code": "pa"} in r["sort"]
    assert r["subcategories"][0]["slug"].startswith("car/")
    by = {f["label"]: f for f in r["filters"]}
    assert by["سال تولید"]["range"] == ["mn68101", "mx68101"]
    assert by["نقدی/اقساطی"]["name"] == "a69150" and {"id": "445100", "label": "اقساطی"} in by["نقدی/اقساطی"]["options"]


async def test_request_looks_like_the_web_app(api):
    api[SEARCH] = fixture("search.json")
    await http.fetch("search/tehran/car")
    h = api.calls[0].headers
    assert h["accept"] == "application/vnd.api+json" and h["referer"] == "https://www.sheypoor.com/"


async def test_host_allowlist_and_http_errors(api):
    with pytest.raises(http.ApiError):
        await http.fetch("//evil.example/x")
    api["/api/v10.0.0/a"] = lambda r: httpx.Response(403, text="no")
    with pytest.raises(http.ApiError, match="blocked"):
        await http.fetch("a")


async def test_search_areas_matching_and_rent_prices(client, api):
    body = fixture("search_rent.json")
    api[RENT] = body
    r = (
        await client.call_tool(
            "sp_search",
            {"city": "tehran", "category": "house-apartment-for-rent", "neighbourhoods": ["888", "974"]},
        )
    ).structured_content
    assert api.calls[0].url.query.count(b"nh%5B%5D=") == 2
    assert r["matching"] == body["meta"]["normal_count"]
    first = r["ads"][0]
    assert set(first["prices"]) == {"رهن", "اجاره"} and first["url"].endswith(f"/v/{first['id']}")
    bad = await client.call_tool("sp_search", {"neighbourhoods": ["x"]})
    assert bad.is_error


async def test_model_path_is_accepted(client, api):
    api["/api/v10.0.0/search/tehran/car/peugeot/206"] = fixture("search.json")
    r = await client.call_tool("sp_search", {"city": "tehran", "category": "car/peugeot/206"})
    assert not r.is_error


async def test_filters_cover_rent_ranges_booleans_and_areas(client, api):
    api["/api/v10.0.0/search/filters/tehran/house-apartment-for-rent"] = fixture("filters_rent.json")
    r = (
        await client.call_tool("sp_filters", {"category": "house-apartment-for-rent", "city": "tehran"})
    ).structured_content
    by = {f["label"]: f for f in r["filters"]}
    assert by["رهن"]["range"] == ["mn68090", "mx68090"] and by["اجاره"]["range"] == ["mn68092", "mx68092"]
    assert by["متراژ"]["range"][0] == "mn68085" and by["پارکینگ"]["value"] == "1"
    assert r["subcategories"] == [] and len(r["neighbourhoods"]) == 4
    assert {s["code"] for s in r["sort"]} == {"n"}  # nearest-first is not offered
    one = (
        await client.call_tool("sp_filters", {"category": "house-apartment-for-rent", "city": "tehran", "area": "پونک"})
    ).structured_content
    assert [a["name"] for a in one["neighbourhoods"]] == ["پونک"]


async def test_similar_widens_and_excludes_itself(client, api):
    api["/api/v10.0.0/listings/468673507"] = fixture("listing.json")
    seen = []

    def search(request):
        seen.append(request.url.path)
        body = fixture("search.json")
        if request.url.path.endswith("/creta"):
            body["data"] = body["data"][:0]
        return httpx.Response(200, json=body)

    api["/api/v10.0.0/search/tehran/car/hyundai/creta"] = search
    api["/api/v10.0.0/search/tehran/car/hyundai"] = search
    r = (await client.call_tool("sp_similar", {"id": "468673507"})).structured_content
    assert seen == ["/api/v10.0.0/search/tehran/car/hyundai/creta", "/api/v10.0.0/search/tehran/car/hyundai"]
    assert r["category"] == "car/hyundai" and r["price_band"] == [6272000000, 11648000000]
    assert r["ads"] and all(a["id"] != "468673507" for a in r["ads"])
    q = dict(api.calls[-1].url.params)
    assert q["mnp"] == "6272000000" and q["mxp"] == "11648000000"


async def test_shops_and_shop(client, api):
    api["/api/v10.0.0/shops/tehran/real-estate"] = fixture("shops.json")
    r = (
        await client.call_tool("sp_shops", {"city": "tehran", "category": "real-estate", "limit": 3})
    ).structured_content
    assert r["total"] > 100 and r["shops"][0]["slug"] and r["shops"][0]["url"].startswith("https://")
    assert dict(api.calls[0].url.params)["page[size]"] == "3"
    # the real page lists an expired shop (0 ads) whose shops/slug/<slug> answers 404; it must not be offered
    assert len(r["shops"]) == 2 and all(x["ads"] for x in r["shops"])
    api["/api/v10.0.0/shops/slug/amlakalijenabtehran"] = fixture("shop_detail.json")
    api["/api/v10.0.0/shops/slug/amlakalijenabtehran/listings"] = fixture("shop_listings.json")
    s = (await client.call_tool("sp_shop", {"slug": "amlakalijenabtehran"})).structured_content
    assert s["name"] == "املاک عالیجناب" and len(s["agents"]) == 7 and s["ads_total"] == 2
    assert s["ads"][1]["prices"] == {"رهن": 13000000000, "اجاره": "توافقی"} and "primaryPhone" not in str(s)
    api.clear()
    assert (await client.call_tool("sp_shop", {"slug": "nope"})).is_error


async def test_car_prices_parse_embedded_list(client, api):
    api["/car-price/saipa-group"] = lambda request: httpx.Response(
        200, text=(FIXTURES / "carprice.html").read_text(encoding="utf-8")
    )
    r = (await client.call_tool("sp_car_prices", {"group": "saipa-group", "limit": 2})).structured_content
    assert r["unit"] == "Toman" and r["models"] >= 2
    y = r["cars"][0]["years"][0]
    assert isinstance(y["market"], int) and y["market"] > 10**8 and y["year"].isdigit()
    assert api.calls[0].headers["sec-fetch-mode"] == "navigate"
    name = r["cars"][0]["model"].split()[0]
    assert (await client.call_tool("sp_car_prices", {"group": "saipa-group", "query": name})).structured_content["cars"]
    assert (await client.call_tool("sp_car_prices", {"group": "saipa-group", "query": "zzzz"})).structured_content[
        "models"
    ] == 0
    api["/car-price/saipa-group"] = lambda request: httpx.Response(200, text="<html></html>")
    http._cache.clear()
    assert (await client.call_tool("sp_car_prices", {"group": "saipa-group"})).is_error


async def test_area_prices_rent_and_sale(client, api):
    api["/api/v10.0.0/search/filters/tehran/house-apartment-for-rent"] = fixture("filters_rent.json")
    api[RENT] = fixture("search_rent.json")
    r = (
        await client.call_tool(
            "sp_area_prices", {"city": "tehran", "category": "house-apartment-for-rent", "neighbourhoods": ["888"]}
        )
    ).structured_content
    a = r["areas"][0]
    assert a["id"] == "888" and a["name"] == "اختیاریه" and a["sampled"] == 3
    assert a["median_deposit"] and a["median_rent"] and "median_price" not in a
    assert dict(api.calls[-1].url.params)["nh[]"] == "888"
    top = (
        await client.call_tool("sp_area_prices", {"city": "tehran", "category": "house-apartment-for-rent", "top": 2})
    ).structured_content
    assert [x["id"] for x in top["areas"]] == ["888", "974"]  # busiest first


async def test_headers_match_the_web_app(api):
    api["/api/v10.0.0/general/locations"] = {"ok": 1}
    api[SEARCH] = {"ok": 1}
    await http.fetch("general/locations")
    await http.fetch("search/tehran/car")
    lookup, search = (c.headers for c in api.calls)
    assert lookup["accept"] == "*/*" and "content-type" not in lookup
    assert search["accept"] == search["content-type"] == "application/vnd.api+json"
    for h in (lookup, search):
        assert h["referer"] == "https://www.sheypoor.com/" and h["sec-fetch-site"] == "same-origin"
        assert "origin" not in h and h["accept-language"].startswith("fa-IR")
