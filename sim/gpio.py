"""Pure validation of the currently supported public GPIO pin spelling."""

import re


def validate_gpio_pin(pin: str) -> str:
    """Accept the public GPIO spelling A0 through H15 without normalization."""
    if not isinstance(pin, str):
        raise TypeError("GPIO pin must be a string")
    if re.fullmatch(r"[A-H](?:[0-9]|1[0-5])", pin) is None:
        raise ValueError("GPIO pin must be A0 through H15")
    return pin
