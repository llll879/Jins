"""Быстрый старт:  JINS_URL=https://your-host JINS_KEY=jns_... python quickstart.py @username"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sdk"))
from jins_client import Jins, JinsError

jins = Jins(os.environ["JINS_URL"], os.environ["JINS_KEY"])
try:
    print("Ключ:", jins.me()["key"]["name"], "| права:", jins.me()["key"]["scopes"])
    r = jins.check(sys.argv[1] if len(sys.argv) > 1 else "@username")
    print("Проверка:", r)
    if r["verdict"] in ("scam", "risk"):
        print("⚠️ Осторожно со сделкой!")
except JinsError as e:
    print("Ошибка API:", e)
