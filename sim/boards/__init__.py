"""Public board adapters; peripheral state remains owned by the board."""

from .qemu import QemuBoard, validate_gpio_pin

__all__ = ["QemuBoard", "validate_gpio_pin"]
