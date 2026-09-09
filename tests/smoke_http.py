"""Exercise a disposable deployment over HTTP. Creates one test account and task."""
import http.cookiejar
import re
import secrets
import sys
import urllib.parse
import urllib.request


def main(base="http://localhost:8081"):
    browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def get(path):
        return browser.open(base+path, timeout=10).read().decode()
    def submit(path, form_page, **fields):
        token = re.search(r'name="csrf" value="([^"]+)"', get(form_page)).group(1)
        data = urllib.parse.urlencode({"csrf": token, **fields}).encode()
        return browser.open(base+path, data=data, timeout=10).read().decode()
    assert '"ready"' in get("/health/ready")
    username = "smoke_"+secrets.token_hex(6)
    password = secrets.token_urlsafe(24)
    submit("/register", "/register", username=username, password=password)
    submit("/login", "/login", username=username, password=password)
    html = submit("/tasks", "/", title="Smoke test: deployed persistence", priority="high")
    assert "Smoke test: deployed persistence" in get("/")
    task_id = re.search(r'/tasks/([a-f0-9]+)/complete', html).group(1)
    submit(f"/tasks/{task_id}/complete", "/", version="1")
    assert "Smoke test: deployed persistence" in get("/?status=done")
    submit(f"/tasks/{task_id}/delete", "/", version="2")
    assert "Smoke test: deployed persistence" not in get("/")
    submit("/logout", "/")
    assert "Welcome back." in get("/")
    print("HTTP smoke test passed: readiness, signup, login, create, reload, complete, delete, logout.")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv)>1 else "http://localhost:8081")
