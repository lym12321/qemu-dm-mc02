"""Protocol-neutral byte echo fixture; no framing, transport or clock owner."""

from sim.api import NodeDescription, PortSpec


class UartEcho:
    def __init__(self, context):
        self._closed = False
        self._connection = context.attach('uart', self)

    def receive_uart(self, data):
        if self._closed:
            raise RuntimeError('UART echo is closed')
        self._connection.send(data)

    def close(self):
        self._closed = True


class UartEchoFactory:
    @staticmethod
    def describe(spec):
        if spec.options:
            raise ValueError('UART echo has no options')
        return NodeDescription((PortSpec('uart', 'uart', min_connections=1),))

    @staticmethod
    def create(spec, context):
        return UartEcho(context)
