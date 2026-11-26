"""Sample proportional set size per browser process while a probe run is in flight."""
import json
import os
import sys
import time

TYPES = (('--type=renderer', 'renderer'), ('--type=gpu-process', 'gpu'), ('--type=utility', 'utility'),
         ('--type=zygote', 'zygote'), ('crashpad', 'crashpad'), ('google-chrome', 'browser'),
         ('Xvfb', 'xvfb'), ('mem_sample', 'sampler'))


def classify(cmdline):
    for pattern, name in TYPES:
        if pattern in cmdline:
            return name
    return 'python' if 'python' in cmdline else 'other'


def pss_kb(pid):
    try:
        with open(f'/proc/{pid}/smaps_rollup') as handle:
            for line in handle:
                if line.startswith('Pss:'):
                    return int(line.split()[1])
    except OSError:
        return 0
    return 0


def memory_stat():
    totals = {}
    try:
        with open('/sys/fs/cgroup/memory.stat') as handle:
            for line in handle:
                key, value = line.split()
                if key in ('anon', 'file', 'shmem', 'kernel'):
                    totals[key] = int(value) // 1024
    except OSError:
        pass
    return totals


out, stop = sys.argv[1], sys.argv[2]
deadline = time.monotonic() + float(sys.argv[3] if len(sys.argv) > 3 else 120)
rows = []
while not os.path.exists(stop) and time.monotonic() < deadline:
    procs = {}
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        try:
            with open(f'/proc/{entry}/cmdline', 'rb') as handle:
                cmdline = handle.read().decode('utf-8', 'replace').replace('\0', ' ')
        except OSError:
            continue
        value = pss_kb(entry)
        if value:
            procs[classify(cmdline)] = procs.get(classify(cmdline), 0) + value
    rows.append({'t': round(time.monotonic(), 1), 'procs_kb': procs, 'cgroup_kb': memory_stat()})
    time.sleep(1.5)
with open(out, 'w') as handle:
    json.dump(rows, handle)
