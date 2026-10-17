import json
import os
from consumer import display, unrelated_discount


def test_display_ten_dollars():
    assert display(json.loads(os.environ['SW_RESPONSE'])) == '$10.00'


def test_unrelated_conversion():
    assert unrelated_discount(250) == 2.5
