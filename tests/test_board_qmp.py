"""Host adapter admission; native GPIO/QMP semantics have separate qtests."""

from copy import deepcopy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim.boards import QemuBoard, validate_gpio_pin


def native_state():
    # Literal native schema 1, including intentionally distinct host key input
    # and sampled level. Expected values do not call the production validator.
    return {
        "schema": 1, "timestamp_ns": 123456789, "board": "DM-MC02",
        "vin_mv": 24000,
        "gpio": {
            "A": {"moder": 0x4000, "sampled": 0x80, "odr": 0x80,
                  "output_mask": 0x80},
            "B": {"moder": 0x1e4, "sampled": 0x0f, "odr": 2,
                  "output_mask": 0x12},
            "C": {"moder": 0, "sampled": 0, "odr": 0, "output_mask": 0},
            "D": {"moder": 0, "sampled": 0, "odr": 0, "output_mask": 0},
            "E": {"moder": 0, "sampled": 0, "odr": 0, "output_mask": 0},
            "F": {"moder": 0, "sampled": 0, "odr": 0, "output_mask": 0},
            "G": {"moder": 0, "sampled": 0, "odr": 0, "output_mask": 0},
            "H": {"moder": 0, "sampled": 0, "odr": 0, "output_mask": 0},
        },
        "led": {"pin": "A7", "rgb": 0xffffff, "brightness": 255,
                "source": "gpio-latch"},
        "key": {"pin": "A15", "pressed": False, "level": 0},
        "extension": {"native": True},
    }


class Qmp:
    def __init__(self, value=None):
        self.value = native_state() if value is None else value
        self.commands = []
        self.closed = False

    def command(self, name, arguments=None):
        self.commands.append((name, arguments))
        return self.value

    def close(self):
        self.closed = True


def test_literal_snapshot_and_digital_aliases_preserve_native_fields():
    qmp = Qmp()
    board = QemuBoard(qmp)
    before = deepcopy(qmp.value)
    state = board.snapshot()
    assert state is qmp.value
    assert state == before
    assert qmp.commands == [("qom-get", {
        "path": "/machine", "property": "board-state",
    })]
    assert board.digital("gpio.B0", state) == 1
    assert board.digital("gpio.B4", state) == 0
    assert board.digital("key", state) == 0
    assert board.digital("led", state) == 1
    state["led"].update(rgb=0, brightness=0, source="ws2812-dma")
    assert board.digital("led", state) == 0
    assert len(qmp.commands) == 1
    assert not qmp.closed


@pytest.mark.parametrize("method,args", [
    ("set_gpio", ("I0", 1)), ("set_gpio", ("A16", 1)),
    ("set_gpio", ("A01", 1)), ("set_gpio", ("a1", 1)),
    ("set_gpio", ("A0", True)), ("set_gpio", ("A0", 2)),
    ("set_key", (1,)), ("set_vin", (-1,)),
    ("set_vin", (0x100000000,)), ("set_vin", (True,)),
    ("digital", ("gpio.H16",)), ("digital", ("gpio.B0.extra",)),
    ("digital", ("register.ODR",)), ("digital", (0,)),
])
def test_input_admission_precedes_qmp_command(method, args):
    qmp = Qmp()
    board = QemuBoard(qmp)
    with pytest.raises((TypeError, ValueError)):
        getattr(board, method)(*args)
    assert qmp.commands == []


def test_native_producer_values_and_borrowed_connection():
    qmp = Qmp()
    board = QemuBoard(qmp)
    assert validate_gpio_pin("H15") == "H15"
    board.set_gpio("H15", 1)
    board.set_key(True)
    board.set_vin(0xffffffff)
    board.pause()
    board.resume()
    board.reset()
    assert qmp.commands == [
        ("qom-set", {"path": "/machine", "property": "gpio-input",
                     "value": "H15=1"}),
        ("qom-set", {"path": "/machine", "property": "user-key", "value": True}),
        ("qom-set", {"path": "/machine", "property": "vin-mv", "value": "4294967295"}),
        ("stop", None), ("cont", None), ("system_reset", None),
    ]
    del board
    assert not qmp.closed


@pytest.mark.parametrize("path,value", [
    (("schema",), True), (("schema",), 2), (("timestamp_ns",), -1),
    (("timestamp_ns",), 0.0), (("vin_mv",), 0x100000000),
    (("gpio", "B", "moder"), 0x100000000),
    (("gpio", "B", "sampled"), True),
    (("gpio", "B", "odr"), -1),
    (("gpio", "B", "output_mask"), 0x10000),
    (("led", "rgb"), 0x1000000), (("led", "brightness"), 256),
    (("led", "source"), "electrical"), (("led", "pin"), "I0"),
    (("key", "pin"), "H16"), (("key", "pressed"), 1),
    (("key", "level"), True), (("key", "level"), 2),
])
def test_malformed_native_values_are_rejected(path, value):
    state = native_state()
    container = state
    for part in path[:-1]:
        container = container[part]
    container[path[-1]] = value
    with pytest.raises((TypeError, ValueError)):
        QemuBoard(Qmp(state)).snapshot()


def test_missing_native_field_and_supplied_snapshot_are_validated():
    qmp = Qmp()
    board = QemuBoard(qmp)
    state = native_state()
    del state["gpio"]["B"]["sampled"]
    with pytest.raises(ValueError, match="gpio.B.sampled"):
        board.digital("gpio.B0", state)
    assert qmp.commands == []


@pytest.mark.parametrize("port", ["H", "I"])
def test_native_gpio_geometry_requires_all_eight_banks(port):
    state = native_state()
    if port == "H":
        del state["gpio"][port]
    else:
        state["gpio"][port] = {}
    with pytest.raises(ValueError, match="exactly banks A through H"):
        QemuBoard(Qmp(state)).snapshot()


def test_qmp_errors_propagate_without_transport_cleanup():
    failure = RuntimeError("native QMP rejection")

    class FailedQmp(Qmp):
        def command(self, name, arguments=None):
            raise failure

    qmp = FailedQmp()
    board = QemuBoard(qmp)
    with pytest.raises(RuntimeError) as caught:
        board.snapshot()
    assert caught.value is failure
    with pytest.raises(RuntimeError) as caught:
        board.set_key(False)
    assert caught.value is failure
    assert not qmp.closed
    with pytest.raises(TypeError, match="callable"):
        QemuBoard(object())
