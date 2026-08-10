"""检查地图标记是否含可用的文字描述。"""
import json
import urllib.parse
import urllib.request

req = urllib.request.Request(
    "https://gta.fandom.com/api.php?" + urllib.parse.urlencode({
        "action": "query", "format": "json", "formatversion": "2",
        "prop": "revisions", "rvprop": "content", "rvslots": "main",
        "titles": "Map:GTA San Andreas: Oysters"}),
    headers={"User-Agent": "SA-Agent/0.1"})
d = json.load(urllib.request.urlopen(req, timeout=45))
txt = d["query"]["pages"][0]["revisions"][0]["slots"]["main"]["content"]
m = json.loads(txt)

print("顶层键:", list(m.keys()))
markers = m.get("markers", [])
print("标记数:", len(markers))
for k in markers[:3]:
    print("-" * 50)
    print(json.dumps(k, ensure_ascii=False, indent=2)[:700])

withdesc = [k for k in markers
            if (k.get("description") or "").strip()
            or (k.get("popup", {}) or {}).get("description")]
print("\n含 description 的标记:", len(withdesc), "/", len(markers))
