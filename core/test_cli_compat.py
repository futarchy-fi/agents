import sys
from decimal import Decimal
from pathlib import Path

from core.api_models import BuyRequest, SellRequest


CLI_ROOT = Path(__file__).resolve().parents[1] / "cli"
if str(CLI_ROOT) not in sys.path:
    sys.path.insert(0, str(CLI_ROOT))

from futarchy_cli.api import Client


class _CaptureClient(Client):
    def __init__(self):
        super().__init__(api_url="http://test")
        self.captured = None

    def post(self, path: str, body: dict) -> dict:
        self.captured = {"path": path, "body": body}
        return body


def test_cli_buy_serializes_budget_as_string_matching_api_model():
    client = _CaptureClient()
    payload = client.buy(45, "no", Decimal("1.25"))

    assert client.captured == {
        "path": "/v1/markets/45/buy",
        "body": {"outcome": "no", "budget": "1.25"},
    }
    assert BuyRequest(**payload).budget == "1.25"
    client._http.close()


def test_cli_sell_serializes_amount_as_string_matching_api_model():
    client = _CaptureClient()
    payload = client.sell(45, "yes", Decimal("2.5"))

    assert client.captured == {
        "path": "/v1/markets/45/sell",
        "body": {"outcome": "yes", "amount": "2.5"},
    }
    assert SellRequest(**payload).amount == "2.5"
    client._http.close()
