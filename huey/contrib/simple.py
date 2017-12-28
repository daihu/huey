import gevent
from gevent import socket
from gevent.pool import Pool
from gevent.server import StreamServer

from collections import defaultdict
from collections import deque
from collections import namedtuple
from io import BytesIO
from socket import error as socket_error
from socket import EINTR
import datetime
import heapq
import json
import logging
import re
import struct
import sys


logger = logging.getLogger(__name__)


class CommandError(Exception):
    def __init__(self, msg):
        self.msg = msg
        super(CommandError, self).__init__()


class Disconnect(Exception): pass


"""
Protocol is based on Redis wire protocol.

Client sends requests as an array of bulk strings.

Server replies, indicating response type using the first byte:

* "+" - simple string
* "-" - error
* ":" - integer
* "$" - bulk string
* "*" - array

Simple strings: "+string content\r\n"  <-- cannot contain newlines

Error: "-Error message\r\n"

Integers: ":1337\r\n"

Bulk String: "$number of bytes\r\nstring data\r\n"

* Empty string: "$0\r\n\r\n"
* NULL: "$-1\r\n"

Array: "*number of elements\r\n...elements..."

* Empty array: "*0\r\n"
"""
if sys.version_info[0] == 3:
    unicode = str
    basestring = (bytes, str)


Error = namedtuple('Error', ('message',))


class ProtocolHandler(object):
    def __init__(self):
        self.handlers = {
            '+': self.handle_simple_string,
            '-': self.handle_error,
            ':': self.handle_integer,
            '$': self.handle_string,
            '*': self.handle_array,
        }

    def handle_simple_string(self, socket_file):
        return socket_file.readline().rstrip('\r\n')

    def handle_error(self, socket_file):
        return Error(socket_file.readline().rstrip('\r\n'))

    def handle_integer(self, socket_file):
        number = socket_file.readline().rstrip('\r\n')
        if '.' in number:
            return float(number)
        return int(number)

    def handle_string(self, socket_file):
        length = int(socket_file.readline().rstrip('\r\n'))
        if length == -1:
            return None
        length += 2
        return socket_file.read(length)[:-2]

    def handle_array(self, socket_file):
        num_elements = int(socket_file.readline().rstrip('\r\n'))
        return [self.handle_request(socket_file) for _ in range(num_elements)]

    def handle_request(self, socket_file):
        first_byte = socket_file.read(1)
        if not first_byte:
            raise Disconnect()

        try:
            return self.handlers[first_byte](socket_file)
        except KeyError:
            raise ValueError('invalid request')

    def write_response(self, socket_file, data):
        buf = BytesIO()
        self._write(buf, data)
        buf.seek(0)
        socket_file.write(buf.getvalue())
        socket_file.flush()

    def _write(self, buf, data):
        if isinstance(data, bytes):
            buf.write('$%s\r\n%s\r\n' % (len(data), data))
        elif isinstance(data, unicode):
            bdata = data.encode('utf-8')
            buf.write('$%s\r\n%s\r\n' % (len(bdata), data))
        elif isinstance(data, (int, float)):
            buf.write(':%s\r\n' % data)
        elif isinstance(data, Error):
            buf.write('-%s\r\n' % data.message)
        elif isinstance(data, (list, tuple)):
            buf.write('*%s\r\n' % len(data))
            for item in data:
                self._write(buf, item)
        elif data is None:
            buf.write('$-1\r\n')


class Shutdown(Exception): pass


class QueueServer(object):
    def __init__(self, host='127.0.0.1', port=31337, max_clients=64):
        self._host = host
        self._port = port
        self._max_clients = max_clients
        self._pool = Pool(max_clients)
        self._server = StreamServer(
            (self._host, self._port),
            self.connection_handler,
            spawn=self._pool)

        self._commands = self.get_commands()
        self._protocol = ProtocolHandler()

        self._kv = {}
        self._queues = defaultdict(deque)
        self._schedule = []

    def get_commands(self):
        timestamp_re = (r'(?P<timestamp>\d{4}-\d{2}-\d{2} '
                        '\d{2}:\d{2}:\d{2}(?:\.\d+)?)')
        return dict((
            # Queue commands.
            ('ENQUEUE', self.queue_append),
            ('DEQUEUE', self.queue_pop),
            ('REMOVE', self.queue_remove),
            ('FLUSH', self.queue_flush),
            ('LENGTH', self.queue_length),

            # K/V commands.
            ('SET', self.kv_set),
            ('SETNX', self.kv_setnx),
            ('GET', self.kv_get),
            ('POP', self.kv_pop),
            ('DELETE', self.kv_delete),
            ('EXISTS', self.kv_exists),
            ('FLUSH_KV', self.kv_flush),
            ('LENGTH_KV', self.kv_length),

            # Schedule commands.
            ('ADD', self.schedule_add),
            ('READ', self.schedule_read),
            ('READ', self.schedule_read),
            ('FLUSH_SCHEDULE', self.schedule_flush),
            ('LENGTH_SCHEDULE', self.schedule_length),

            # Misc.
            ('FLUSHALL', self.flush_all),
            ('SHUTDOWN', self.shutdown),
        ))

    def queue_append(self, queue, value):
        self._queues[queue].append(value)
        return 1

    def queue_pop(self, queue):
        try:
            return self._queues[queue].popleft()
        except IndexError:
            pass

    def queue_remove(self, queue, value):
        try:
            self._queues[queue].remove(value)
        except ValueError:
            return 0
        else:
            return 1

    def queue_flush(self, queue):
        qlen = self.queue_length(queue)
        self._queues[queue].clear()
        return qlen

    def queue_length(self, queue):
        return len(self._queues[queue])

    def kv_set(self, key, value):
        self._kv[key] = value
        return 1

    def kv_setnx(self, key, value):
        if key in self._kv:
            return 0
        else:
            self._kv[key] = value
            return 1

    def kv_get(self, key):
        return self._kv.get(key)

    def kv_pop(self, key):
        return self._kv.pop(key, None)

    def kv_delete(self, key):
        if key in self._kv:
            del self._kv[key]
            return 1
        return 0

    def kv_exists(self, key):
        return 1 if key in self._kv else 0

    def kv_flush(self):
        kvlen = self.kv_length()
        self._kv.clear()
        return kvlen

    def kv_length(self):
        return len(self._kv)

    def _decode_timestamp(self, timestamp):
        fmt = '%Y-%m-%d %H:%M:%S'
        if '.' in timestamp:
            fmt = fmt + '.%f'
        try:
            return datetime.datetime.strptime(timestamp, fmt)
        except ValueError:
            raise CommandError('Timestamp must be formatted Y-m-d H:M:S')

    def schedule_add(self, timestamp, data):
        dt = self._decode_timestamp(timestamp)
        heapq.heappush(self._schedule, (dt, data))
        return 1

    def schedule_read(self, timestamp=None):
        dt = self._decode_timestamp(timestamp)
        accum = []
        while self._schedule and self._schedule[0][0] <= dt:
            ts, data = heapq.heappop(self._schedule)
            accum.append(data)
        return accum

    def schedule_flush(self):
        schedulelen = self.schedule_length()
        self._schedule = []
        return schedulelen

    def schedule_length(self):
        return len(self._schedule)

    def flush_all(self):
        self._queues = defaultdict(deque)
        self.kv_flush()
        self.schedule_flush()
        return 1

    def shutdown(self):
        raise Shutdown('shutting down')

    def run(self):
        self._server.serve_forever()

    def connection_handler(self, conn, address):
        logger.info('Connection received: %s:%s' % address)
        socket_file = conn.makefile('rwb')
        while True:
            try:
                data = self._protocol.handle_request(socket_file)
            except Disconnect:
                logger.info('Client went away: %s:%s' % address)
                break

            try:
                resp = self.respond(data)
            except Shutdown:
                logger.info('Shutting down')
                self._protocol.write_response(socket_file, 1)
                raise KeyboardInterrupt()
            except CommandError as command_error:
                resp = Error(command_error.message)
            except Exception as exc:
                logger.exception('Unhandled error')
                resp = Error('Unhandled server error')

            self._protocol.write_response(socket_file, resp)

    def respond(self, data):
        if not isinstance(data, list):
            try:
                data = data.split()
            except:
                raise CommandError('Unrecognized request type.')

        if not isinstance(data[0], basestring):
            raise CommandError('First parameter must be command name.')

        command = data[0].upper()
        if command not in self._commands:
            raise CommandError('Unrecognized command: %s' % command)
        else:
            logger.debug('Received %s', command)

        return self._commands[command](*data[1:])


class Client(object):
    def __init__(self, host='127.0.0.1', port=31337):
        self._host = host
        self._port = port
        self._socket = None
        self._fh = None
        self._protocol = ProtocolHandler()

    def connect(self):
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.connect((self._host, self._port))
        self._fh = self._socket.makefile('rwb')

    def close(self):
        self._socket.close()

    def execute(self, *args):
        self._protocol.write_response(self._fh, args)
        resp = self._protocol.handle_request(self._fh)
        if isinstance(resp, Error):
            raise CommandError(resp.message)
        return resp

    def enqueue(self, queue, data):
        return self.execute('ENQUEUE', queue, data)

    def dequeue(self, queue):
        return self.execute('DEQUEUE', queue)

    def unqueue(self, queue, data):
        return self.execute('REMOVE', queue, data)

    def queue_size(self, queue):
        return self.execute('LENGTH', queue)

    def flush_queue(self, queue):
        return self.execute('FLUSH', queue)

    def add_to_schedule(self, data, ts):
        return self.execute('ADD', str(ts), data)

    def read_schedule(self, ts):
        return self.execute('READ', str(ts))

    def schedule_size(self):
        return self.execute('LENGTH_SCHEDULE')

    def flush_schedule(self):
        return self.execute('FLUSH_SCHEDULE')

    def put_data(self, key, value):
        return self.execute('SET', key, value)
    set = put_data

    def peek_data(self, key):
        return self.execute('GET', key)
    get = peek_data

    def pop_data(self, key):
        return self.execute('POP', key)

    def has_data_for_key(self, key):
        return self.execute('EXISTS', key)

    def put_if_empty(self, key, value):
        return self.execute('SETNX', key, value)

    def result_store_size(self):
        return self.execute('LENGTH_KV')

    def flush_results(self):
        return self.execute('FLUSH_KV')

    def flush_all(self):
        return self.execute('FLUSHALL')

    def shutdown(self):
        self.execute('SHUTDOWN')


if __name__ == '__main__':
    from gevent import monkey; monkey.patch_all()
    logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.DEBUG)
    server = QueueServer()
    server.run()