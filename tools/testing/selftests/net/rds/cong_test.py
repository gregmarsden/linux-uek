#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Manual two-node RDS/TCP congestion-update retry test."""

import argparse
import ctypes
import errno
import os
import select
import socket
import struct
import subprocess
import sys
import time


SOL_RDS = 276
RDS_CONG_MONITOR = 6
SO_RDS_TRANSPORT = 8
RDS_TRANS_TCP = 2
RDS_CMSG_CONG_UPDATE = 5
RDS_INFO_RECV_MESSAGES = 10005

RDS_INFO_MESSAGE = struct.Struct('=QI4s4sHHBB')
FAIL_FUNCTION_DIR = '/sys/kernel/debug/fail_function'
CONG_ALLOC_FUNCTION = 'rds_cong_update_alloc'
PAYLOAD = b'c' * 4096
WARMUP = b'congestion-warmup'
FAILURE_BUDGET = 1000000


class Control:
    """Line-oriented control channel between the two guests."""

    def __init__(self, sock, timeout):
        self.sock = sock
        self.sock.settimeout(timeout)
        self.buffer = bytearray()

    def send(self, line):
        print(f'# control send: {line}', flush=True)
        self.sock.sendall(line.encode('ascii') + b'\n')

    def receive(self, command=None):
        while b'\n' not in self.buffer:
            data = self.sock.recv(4096)
            if not data:
                raise RuntimeError('control connection closed')
            self.buffer.extend(data)

        raw, _, remainder = self.buffer.partition(b'\n')
        self.buffer = bytearray(remainder)
        line = raw.decode('ascii')
        print(f'# control receive: {line}', flush=True)
        if command is not None and line.split(maxsplit=1)[0] != command:
            raise RuntimeError(
                f'expected control command {command}, received {line!r}')
        return line


class FunctionFailure:
    """Inject repeated error returns from one kernel function."""

    def __init__(self, function, error, times):
        self.function = function
        self.error = error
        self.times = times
        self.active = False

    @staticmethod
    def _path(name):
        return os.path.join(FAIL_FUNCTION_DIR, name)

    @classmethod
    def _write(cls, name, value):
        with open(cls._path(name), 'w', encoding='ascii') as config:
            config.write(f'{value}\n')

    def __enter__(self):
        with open(self._path('inject'), encoding='ascii') as functions:
            active = [line.strip() for line in functions if line.strip()]
        if active:
            raise RuntimeError('function fault injection is already in use')

        self._write('probability', 0)
        self._write('times', 0)
        self._write('inject', self.function)
        self.active = True
        try:
            bits = ctypes.sizeof(ctypes.c_ulong) * 8
            retval = hex((-self.error) & ((1 << bits) - 1))
            self._write(f'{self.function}/retval', retval)
            self._write('verbose', 0)
            self._write('task-filter', 'N')
            self._write('interval', 1)
            self._write('space', 0)
            self._write('times', self.times)
            self._write('probability', 100)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def failures(self):
        with open(self._path('times'), encoding='ascii') as config:
            remaining = int(config.read().strip())
        return self.times - remaining

    def __exit__(self, _exc_type, _exc_value, _traceback):
        if not self.active:
            return
        self._write('probability', 0)
        self._write('times', 0)
        self._write('inject', f'!{self.function}')
        self.active = False


def ensure_fault_injection():
    """Mount debugfs if needed and check the allocator injection hook."""
    if not os.path.exists('/sys/kernel/debug'):
        os.makedirs('/sys/kernel/debug', exist_ok=True)
    if not os.path.exists(FAIL_FUNCTION_DIR):
        subprocess.run(
            ['mount', '-t', 'debugfs', 'none', '/sys/kernel/debug'],
            check=False, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)

    injectable = os.path.join(FAIL_FUNCTION_DIR, 'injectable')
    if not os.path.exists(injectable):
        raise RuntimeError(
            'CONFIG_FAIL_FUNCTION and mounted debugfs are required')

    with open(injectable, encoding='ascii') as functions:
        available = [line.split()[0] for line in functions if line.split()]
    if CONG_ALLOC_FUNCTION not in available:
        raise RuntimeError(
            f'{CONG_ALLOC_FUNCTION} is not error injectable')


def load_rds_tcp():
    """Load the RDS/TCP transport when it is built as a module."""
    try:
        subprocess.run(
            ['modprobe', '-q', 'rds_tcp'], check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        pass


def rds_socket(address, port, *, sndbuf=None, rcvbuf=None):
    """Create a nonblocking RDS/TCP socket bound to address and port."""
    sock = socket.socket(socket.AF_RDS, socket.SOCK_SEQPACKET)
    try:
        try:
            sock.setsockopt(SOL_RDS, SO_RDS_TRANSPORT, RDS_TRANS_TCP)
        except OSError as exc:
            if exc.errno == errno.ENOPROTOOPT:
                raise RuntimeError(
                    'RDS/TCP transport is unavailable; enable '
                    'CONFIG_RDS_TCP or load rds_tcp') from exc
            raise
        if sndbuf is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, sndbuf)
        if rcvbuf is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        try:
            sock.bind((address, port))
        except OSError as exc:
            # rds_bind() returns ENOPROTOOPT when the selected transport's
            # local-address check rejects the address.
            if exc.errno in (errno.ENOPROTOOPT, errno.EADDRNOTAVAIL):
                message = f'address {address} is not configured on this node'
                raise RuntimeError(message) from exc
            raise
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        raise


def send_payload(sock, payload, destination, timeout):
    """Retry a nonblocking RDS send while its connection comes up."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            sent = sock.sendto(payload, destination)
            if sent != len(payload):
                raise RuntimeError(f'short RDS send: {sent}/{len(payload)}')
            return
        except BlockingIOError:
            time.sleep(0.01)
        except OSError as exc:
            if exc.errno not in (errno.ECONNRESET, errno.EPIPE,
                                 errno.ENETUNREACH):
                raise
            time.sleep(0.01)
    raise RuntimeError('timed out sending RDS payload')


def receive_payload(sock, expected, timeout):
    """Wait for one RDS payload and compare it with expected."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            payload, _ = sock.recvfrom(max(len(expected), 1) + 64)
        except BlockingIOError:
            time.sleep(0.01)
            continue
        if payload != expected:
            raise RuntimeError(
                f'RDS payload mismatch: expected {expected!r}, '
                f'received {payload!r}')
        return
    raise RuntimeError('timed out receiving RDS payload')


def queued_receive_bytes(sock):
    """Return payload bytes queued on RDS sockets in this network stack."""
    snapshot = sock.getsockopt(
        SOL_RDS, RDS_INFO_RECV_MESSAGES,
        32 * RDS_INFO_MESSAGE.size)
    if len(snapshot) % RDS_INFO_MESSAGE.size:
        raise RuntimeError('invalid RDS receive-message snapshot')
    return sum(message[1] for message in
               RDS_INFO_MESSAGE.iter_unpack(snapshot))


def wait_for_queued_bytes(sock, minimum, timeout):
    """Wait until at least minimum bytes reach the RDS receive queue."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        queued = queued_receive_bytes(sock)
        if queued >= minimum:
            return queued
        time.sleep(0.01)
    raise RuntimeError(
        f'only {queued_receive_bytes(sock)} of {minimum} bytes reached '
        'the RDS receive queue')


def drain_socket(sock, timeout):
    """Drain RDS messages until the receive queue remains empty."""
    deadline = time.monotonic() + timeout
    received = 0
    last_receive = time.monotonic()
    while time.monotonic() < deadline:
        try:
            sock.recvfrom(8192)
            received += 1
            last_receive = time.monotonic()
        except BlockingIOError:
            if received and time.monotonic() - last_receive > 0.25:
                return received
            time.sleep(0.01)
    return received


def wait_for_remote_congestion(sock, destination, timeout):
    """Require the remote congestion map to reject a send with ENOBUFS."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            sock.sendto(b'congestion-probe', destination)
        except BlockingIOError:
            time.sleep(0.01)
        except OSError as exc:
            if exc.errno == errno.ENOBUFS:
                return
            if exc.errno in (errno.ECONNRESET, errno.EPIPE):
                time.sleep(0.01)
                continue
            raise
        else:
            time.sleep(0.01)
    raise RuntimeError(
        'remote congestion was not observed after allocation failures')


def wait_for_notification(sock, remote_port, timeout):
    """Require poll and RDS_CMSG_CONG_UPDATE for an uncongested peer."""
    poller = select.poll()
    poller.register(sock, select.POLLIN)
    events = poller.poll(int(timeout * 1000))
    if not events or not events[0][1] & select.POLLIN:
        raise RuntimeError('no uncongested notification poll wakeup')

    _, ancillary, _, _ = sock.recvmsg(
        1, socket.CMSG_SPACE(8), socket.MSG_DONTWAIT)
    notifications = [
        data for level, cmsg_type, data in ancillary
        if level == SOL_RDS and cmsg_type == RDS_CMSG_CONG_UPDATE
    ]
    if not notifications:
        raise RuntimeError('RDS_CMSG_CONG_UPDATE was not delivered')

    notify_mask = struct.unpack('=Q', notifications[0][:8])[0]
    expected_mask = 1 << (remote_port % 64)
    if not notify_mask & expected_mask:
        raise RuntimeError(
            f'wrong congestion mask: {notify_mask:#x}, '
            f'expected bit {expected_mask:#x}')


def connect_control(address, port, timeout):
    """Connect to the receiver control listener with retries."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return socket.create_connection((address, port), timeout=1)
        except OSError:
            time.sleep(0.1)
    raise RuntimeError('timed out connecting to receiver control socket')


def run_receiver(args):
    """Run the fault-injecting endpoint."""
    ensure_fault_injection()
    rds = rds_socket(args.receiver_address, args.receiver_port,
                     rcvbuf=args.receive_buffer)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((args.receiver_address, args.control_port))
    listener.listen(1)
    listener.settimeout(args.timeout)

    print(f'# receiver listening on {args.receiver_address}', flush=True)
    try:
        control_sock, peer = listener.accept()
        print(f'# control connection from {peer[0]}', flush=True)
        with control_sock:
            control = Control(control_sock, args.timeout)
            rcvbuf = rds.getsockopt(
                socket.SOL_SOCKET, socket.SO_RCVBUF) // 2
            prefill_count = rcvbuf // len(PAYLOAD)
            if prefill_count == 0:
                raise RuntimeError(
                    f'RDS receive buffer {rcvbuf} is too small')
            prefill_bytes = prefill_count * len(PAYLOAD)

            control.send(f'READY {rcvbuf} {prefill_count}')
            control.receive('WARMUP')
            receive_payload(rds, WARMUP, args.timeout)
            time.sleep(1)
            control.send('WARMUP_OK')

            control.receive('PREFILL')
            queued = wait_for_queued_bytes(
                rds, prefill_bytes, args.timeout)
            if queued > rcvbuf:
                raise RuntimeError(
                    'receiver became congested before fault injection')
            control.send('PREFILL_OK')

            with FunctionFailure(CONG_ALLOC_FUNCTION, errno.ENOMEM,
                                 FAILURE_BUDGET) as fault:
                control.send('ARMED')
                control.receive('CROSS')
                queued = wait_for_queued_bytes(
                    rds, prefill_bytes + len(PAYLOAD), args.timeout)
                if queued <= rcvbuf:
                    raise RuntimeError(
                        'receiver did not cross its congestion threshold')

                deadline = time.monotonic() + args.timeout
                while fault.failures() == 0:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            'congestion-update allocation failure was not '
                            'injected')
                    time.sleep(0.01)
                time.sleep(args.failure_window)
                failures = fault.failures()

            control.send(f'INJECTION_DONE {failures}')
            control.receive('CONGESTED')

            received = drain_socket(rds, args.timeout)
            if received == 0:
                raise RuntimeError('no congesting payloads were received')
            control.send(f'DRAINED {received}')
            control.receive('PASS')
    finally:
        listener.close()
        rds.close()


def run_sender(args):
    """Run the endpoint that validates remote congestion notifications."""
    rds = rds_socket(args.sender_address, args.sender_port,
                     sndbuf=4 * 1024 * 1024)
    try:
        rds.setsockopt(SOL_RDS, RDS_CONG_MONITOR, 1)
        with connect_control(args.receiver_address, args.control_port,
                             args.timeout) as control_sock:
            control = Control(control_sock, args.timeout)
            ready = control.receive('READY').split()
            if len(ready) != 3:
                raise RuntimeError(f'invalid READY response: {ready!r}')
            prefill_count = int(ready[2])
            destination = (args.receiver_address, args.receiver_port)

            send_payload(rds, WARMUP, destination, args.timeout)
            control.send('WARMUP')
            control.receive('WARMUP_OK')

            for _ in range(prefill_count):
                send_payload(rds, PAYLOAD, destination, args.timeout)
            control.send(f'PREFILL {prefill_count}')
            control.receive('PREFILL_OK')

            control.receive('ARMED')
            send_payload(rds, PAYLOAD, destination, args.timeout)
            control.send('CROSS')
            control.receive('INJECTION_DONE')

            wait_for_remote_congestion(rds, destination, args.timeout)
            control.send('CONGESTED')
            control.receive('DRAINED')
            wait_for_notification(rds, args.receiver_port, args.timeout)
            control.send('PASS')
    finally:
        rds.close()


def parse_args():
    """Parse the endpoint role and shared topology options."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('role', choices=('receiver', 'sender'))
    parser.add_argument('--sender-address', default='10.23.0.1')
    parser.add_argument('--receiver-address', default='10.23.0.2')
    parser.add_argument('--sender-port', type=int, default=4300)
    parser.add_argument('--receiver-port', type=int, default=5400)
    parser.add_argument('--control-port', type=int, default=6400)
    parser.add_argument('--receive-buffer', type=int, default=16384)
    parser.add_argument('--failure-window', type=float, default=0.5)
    parser.add_argument('--timeout', type=float, default=20)
    return parser.parse_args()


def main():
    """Run one endpoint and report a shell-friendly result."""
    args = parse_args()
    try:
        load_rds_tcp()
        if args.role == 'receiver':
            run_receiver(args)
        else:
            run_sender(args)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        print(f'FAIL: {exc}', file=sys.stderr, flush=True)
        return 1

    print(f'PASS: RDS congestion retry ({args.role})', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
