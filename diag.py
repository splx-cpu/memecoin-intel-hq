import sys, re
sys.path.insert(0, "src")
from mihq.config import load_settings
import psycopg

s = load_settings()
u = s.database_url or ""
mask = lambda t: re.sub(r"://([^:]+):[^@]+@", r"://\1:***@", t)

print("URL as read :", mask(u))
print("length      :", len(u))
try:
    c = psycopg.connect(u, connect_timeout=10, sslmode="require")
    print("RESULT      : CONNECTED OK")
    c.close()
except Exception as e:
    print("ERROR TYPE  :", type(e).__name__)
    print("DETAIL      :", mask(str(e)))
