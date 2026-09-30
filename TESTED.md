# RDS/TCP forward-port test record

Date: 2026-09-30
Base: `net-next/main` at `47a144672573`
Branch: `rds-tcp-upstream-ready`

Testing was performed in the supplied arm64 QEMU/KVM environment. The host
was used only to build the kernel and launch QEMU; selftests were run inside
the guest.

## Kernel build

Cross compiler:

```text
/home/gmarsden/git/linux-uek/net-next/buildroot/output/host/bin/aarch64-buildroot-linux-gnu-
```

Command:

```text
make ARCH=arm64 CROSS_COMPILE=/home/gmarsden/git/linux-uek/net-next/buildroot/output/host/bin/aarch64-buildroot-linux-gnu- -j8 Image
```

Result: successful. The final image reported:

```text
Linux version 7.3.0-rc4+ (gmarsden@fedora) (aarch64-buildroot-linux-gnu-gcc.br_real (Buildroot 2026.08-rc2-45-g211cfafa16) 15.3.0, GNU ld (GNU Binutils) 2.45.1) #18 SMP PREEMPT_DYNAMIC Wed Sep 30 08:04:25 PDT 2026
```

## QEMU configuration

The final VM was launched with:

```text
qemu-system-aarch64 -cpu host -M virt -enable-kvm -kernel arch/arm64/boot/Image -drive file=/tmp/rds-rootfs-final.ext2,format=raw,if=virtio -append "root=/dev/vda console=ttyAMA0" -virtfs local,path=.,mount_tag=shared_kernel,security_model=none,id=shared_kernel -netdev socket,id=n1,udp=127.0.0.1:1235,localaddr=127.0.0.1:1234 -device virtio-net-pci,netdev=n1 -m 2G -nographic
```

The final VM booted the built image and reported:

```text
[    0.151934] NET: Registered PF_RDS protocol family
[    0.152017] Registered RDS/tcp transport
Mounted net-next at /mnt/net-next
```

The two-VM congestion run booted the same final source tree as kernel build
`#19 SMP PREEMPT_DYNAMIC Wed Sep 30 08:09:32 PDT 2026`.

## Per-peer send-buffer selftest, patch 0007

Guest command:

```text
python3 test.py -T tcp -t 60
```

Baseline, before patches 0006-0007:

```text
ok 1 rds selftest tcp
not ok 2 rds selftest tcp per-peer send buffer
stalled peer exhausted the healthy peer buffer
GUEST_RC=1
```

After patches 0006-0007:

```text
ok 1 rds selftest tcp
ok 2 rds selftest tcp per-peer send buffer
# Totals: pass:2 fail:0 skip:0
GUEST_RC=0
```

Result: patch 0007 retained.

## TCP lifecycle selftest, patch 0016

For the baseline, the authored lifecycle test was run from an isolated source
tree containing the test but not lifecycle implementation patches 0008-0015:

```text
python3 tcp_lifecycle.py --case all --logdir /tmp/rds-life-base --hz 100
```

Baseline guest result:

```text
not ok 1 namespace: TCP accept still uses the shared workqueue
case 2 skipped after failed first
# INCOMPLETE: required lifecycle checks were skipped
GUEST_RC=1
```

After patches 0008-0015, the lifecycle-enabled guest run was:

```text
RDS_TEST_HZ=100 python3 test.py -T tcp -t 180
```

The TCP and namespace portions passed, but the reconnect/sendmsg case failed:

```text
ok 1 rds selftest tcp
# TCP lifecycle traces: /mnt/net-next/tools/testing/selftests/net/rds/rds-lifecycle-foeqfd0h
# observed retry delay 7.450s; 22 failed attempts
# FAIL: sendmsg did not cap the pending delay at reconnect_max_delay_ms
not ok 3 rds selftest tcp reconnect backoff and sendmsg shortening
ok 4 rds selftest tcp per-peer send buffer # SKIP stopped after incomplete/failed case
# Totals: pass:2 fail:1 skip:1
# INCOMPLETE: required lifecycle checks were skipped; this is not a passing validation
GUEST_RC=1
```

Result: patch 0016 discarded. Implementation patches 0008-0015 were retained.

## Final TCP/per-peer VM run

With the final retained kernel image, inside the final QEMU guest:

```text
RDS_TEST_HZ=100 python3 test.py -T tcp -t 180
```

Captured result:

```text
TAP version 13
1..2
# getsockopt(): 24/12
# 4/5: ok
# 5/4: ok
# 5/5: ok
# 4/4: ok
# Success
ok 1 rds selftest tcp
# Success
ok 2 rds selftest tcp per-peer send buffer
# Totals: pass:2 fail:0 skip:0
GUEST_RC=0
```

Result: final retained kernel passed the TCP and per-peer selftests in QEMU.

## Congestion notification selftest, patch 0021

This was run with two QEMU/KVM guests using the final built `Image`. The
receiver guest used `10.23.0.2/24`; the sender guest used `10.23.0.1/24`.
Both guests used the supplied UDP socket-network topology with complementary
local/remote ports.

Receiver guest commands:

```text
ip link set eth0 up
ip addr replace 10.23.0.2/24 dev eth0
cd /mnt/net-next/tools/testing/selftests/net/rds
mountpoint -q /sys/kernel/debug || mount -t debugfs none /sys/kernel/debug
python3 cong_test.py receiver
```

Sender guest commands:

```text
ip link set eth0 up
ip addr replace 10.23.0.1/24 dev eth0
cd /mnt/net-next/tools/testing/selftests/net/rds
mountpoint -q /sys/kernel/debug || mount -t debugfs none /sys/kernel/debug
python3 cong_test.py sender
```

Sender result:

```text
# control receive: READY 16384 4
# control send: WARMUP
# control receive: WARMUP_OK
# control send: PREFILL 4
# control receive: PREFILL_OK
# control receive: ARMED
# control send: CROSS
# control receive: INJECTION_DONE 169
# control send: CONGESTED
# control receive: DRAINED 5
# control send: PASS
PASS: RDS congestion retry (sender)
SENDER_RC=0
```

Receiver result:

```text
# control connection from 10.23.0.1
# control send: READY 16384 4
# control receive: WARMUP
# control send: WARMUP_OK
# control receive: PREFILL 4
# control send: PREFILL_OK
# control send: ARMED
# control receive: CROSS
# control send: INJECTION_DONE 169
# control receive: CONGESTED
# control send: DRAINED 5
# control receive: PASS
PASS: RDS congestion retry (receiver)
RECEIVER_RC=0
```

The receiver also logged repeated deferred allocations followed by:

```text
RDS: Deferred congestion update allocated
```

Result: patch 0021 passed in the two-VM test with both roles returning zero.

## Validation exclusions

An earlier host-side selftest attempt occurred before the QEMU-only testing
instruction and failed because host network-namespace setup lacked permission.
It is not counted as validation; all reported passing selftests above ran in
the guest VM.
