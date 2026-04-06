import requests

headers = {
    "x-rapidapi-key": "92113845b9mshb4af7474c102513p1cfa6bjsn877296c66aa2",
    "x-rapidapi-host": "website-seo-analyzer.p.rapidapi.com"
}
r = requests.get(
    "https://website-seo-analyzer.p.rapidapi.com/seo-audit/url",
    headers=headers,
    params={"url": "https://google.com"},
    timeout=30
)
print(r.status_code, r.text[:500])