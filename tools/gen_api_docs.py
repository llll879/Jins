"""Пересобирает openapi.json и таблицу методов в API.md из реестра маршрутов public_api.py.
Запуск (из корня проекта):  python tools/gen_api_docs.py [https://ваш-хост]"""
import json, os, re, sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_public_api  # noqa: F401,E402  (подставляет заглушки aiohttp, если библиотеки не установлены)
import public_api as pa  # noqa: E402

host = sys.argv[1] if len(sys.argv) > 1 else "https://YOUR-HOST"
with open(os.path.join(ROOT, "openapi.json"), "w", encoding="utf-8") as f:
    json.dump(pa.build_openapi(host), f, ensure_ascii=False, indent=2)

rows = ["| Метод | Путь | Область | Описание |", "|---|---|---|---|"]
for s in pa.ROUTES:
    sc = f"`{s['scope']}`" if s["scope"] else ("—" if s["public"] else "любая")
    rows.append(f"| {s['method']} | `{pa.PREFIX}{s['path']}` | {sc} | {s['summary']} |")
table = "\n".join(rows)
p = os.path.join(ROOT, "API.md")
md = open(p, encoding="utf-8").read()
md = re.sub(r"(<!-- ENDPOINTS:START -->\n).*?(\n<!-- ENDPOINTS:END -->)", lambda m: m.group(1) + table + m.group(2), md, flags=re.S)
open(p, "w", encoding="utf-8").write(md)
print(f"openapi.json: {len(pa.ROUTES)} методов; API.md обновлён")
