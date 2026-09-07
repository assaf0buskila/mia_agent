from app.main import app
from fastapi.testclient import TestClient


def test_root_soft_landing_is_200_not_missing_route_json() -> None:
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "https://www.assafweb.com" in response.text
    try:
        payload = response.json()
    except ValueError:
        payload = None
    assert payload != {"detail": "Not Found"}
