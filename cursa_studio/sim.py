"""Simulated GRBL 1.1 + 3018 + warped copper blank, for dry runs of the runner with injected faults."""
import math, random, re
from collections import deque


class FakeGrbl:
    def __init__(self, blank=(102.0, 71.0), g54=(-180.0, -120.0), events=(), seed_tool_off=(0.0, 4.3),
                 backlash=0.0, noise_early=0.0, noise_status=0.0, seed=1):
        self.out = deque()
        self.t = 0.0
        self.m = [0.0, 0.0, -1.0]                     # machine position
        self.g54 = [g54[0], g54[1], 0.0]
        self.abs, self.feed, self.alarm = True, 100.0, False
        self.blank = blank
        self.tool_off = None                          # tip world z = machine z + tool_off
        self.tool_offsets = list(seed_tool_off)
        self.tool = 0
        self.circuit = True
        self.touch_pulse = 0
        self.events = sorted(events)                  # (sim_time_s, 'slip'|'snap'|'drop', mm)
        self.fired = []
        self.prb = (0, 0, 0, 0)
        self.cuts = {1: [], 2: []}                    # (x, y, depth below copper) at G1 end points
        self.rapid_crash = 0
        self.spindle = False
        self.cut_while_stopped = 0
        self.bl = backlash
        self.noise_early = noise_early        # chance a probe move triggers early (reads high)
        self.noise_status = noise_status      # chance a status report / probe start sees a false 'closed'
        self.rng = random.Random(seed)
        self.noise_count = 0
        self.phys = [0.0, 0.0]                       # table X/Y (machine frame) after backlash

    # ---------------------------------------------------------------- world
    def surface(self, xw, yw):
        if not (0 <= xw <= self.blank[0] and 0 <= yw <= self.blank[1]):
            return None
        x, y = xw / self.blank[0] - 0.5, yw / self.blank[1] - 0.5
        return -38.0 + 0.22 * x * x + 0.15 * y * y - 0.12 * x + 0.05 * y + 0.03 * math.sin(xw / 7.0)

    def work(self):
        return [a - b for a, b in zip(self.m, self.g54)]

    def _update_phys(self):
        h = self.bl / 2
        for i in (0, 1):
            if self.m[i] - self.phys[i] > h:
                self.phys[i] = self.m[i] - h
            elif self.phys[i] - self.m[i] > h:
                self.phys[i] = self.m[i] + h

    def phys_work(self):
        return [self.phys[0] - self.g54[0], self.phys[1] - self.g54[1]]

    def tip(self):
        return self.m[2] + self.tool_off

    def clock(self):
        return self.t

    def mount(self, tool):
        self.tool = tool
        self.tool_off = self.tool_offsets[tool - 1]

    def _events(self):
        while self.events and self.events[0][0] <= self.t:
            _, kind, mm = self.events.pop(0)
            if kind in ('slip', 'drop'):
                self.tool_off -= mm                   # tip lower
            elif kind == 'snap':
                self.tool_off += mm                   # tip gone: effective tip much higher
            self.fired.append((round(self.t), kind, mm))

    # ---------------------------------------------------------------- serial interface
    def write(self, data: bytes):
        for ch in data.split(b'\n'):
            if not ch:
                continue
            rt = bytes(c for c in ch if c in b'?!~\x18')
            line = bytes(c for c in ch if c not in b'?!~\x18').decode().strip()
            for c in rt:
                self._realtime(c)
            if line:
                self._line(line)

    def readline(self, timeout):
        return self.out.popleft() if self.out else None

    def _realtime(self, c):
        if c == ord('?'):
            w = self.work()
            pn = ''
            s = self.surface(w[0], w[1])
            touching = self.circuit and s is not None and self.tool_off is not None and self.tip() <= s + 1e-9
            if self.touch_pulse > 0:
                self.touch_pulse -= 1
                touching = self.touch_pulse > 2
            if not touching and self.noise_status and self.rng.random() < self.noise_status:
                touching = True
            if touching:
                pn = '|Pn:P'
            st = 'Alarm' if self.alarm else 'Idle'
            self.out.append(f'<{st}|MPos:{self.m[0]:.3f},{self.m[1]:.3f},{self.m[2]:.3f}|FS:0,0{pn}|'
                            f'WCO:{self.g54[0]:.3f},{self.g54[1]:.3f},{self.g54[2]:.3f}>')
        elif c == 0x18:
            self.alarm = False
            self.spindle = False
            self.out.append("Grbl 1.1h ['$' for help]")

    def ok(self):
        self.out.append('ok')

    # ---------------------------------------------------------------- g-code
    def _line(self, line):
        u = line.upper()
        if u == '$$':
            for k, v in (('$10', 1), ('$20', 0), ('$22', 1), ('$30', 1000), ('$100', 800), ('$101', 800),
                         ('$102', 800), ('$110', 1000), ('$112', 600), ('$120', 30), ('$122', 30)):
                self.out.append(f'{k}={v}')
            return self.ok()
        if u == '$#':
            self.out.append(f'[G54:{self.g54[0]:.3f},{self.g54[1]:.3f},{self.g54[2]:.3f}]')
            self.out.append('[G92:0.000,0.000,0.000]')
            self.out.append('[TLO:0.000]')
            return self.ok()
        if u == '$H':
            self.m = [0.0, 0.0, -1.0]
            self.phys = [0.0, 0.0]
            self.t += 15
            return self.ok()
        if u == '$X':
            self.alarm = False
            return self.ok()
        words = dict()
        g = [float(v) for v in re.findall(r'G([\d.]+)', u)]
        for k, v in re.findall(r'([XYZFPSL])([-\d.]+)', u):
            words[k] = float(v)
        m = [int(v) for v in re.findall(r'M(\d+)', u)]
        if 3 in m:
            self.spindle = True
        if 5 in m:
            self.spindle = False
        if 91 in g:
            self.abs = False
        if 90 in g:
            self.abs = True
        if 'F' in words:
            self.feed = words['F']
        if 10 in g:
            L = int(words.get('L', 2))
            for i, a in enumerate('XYZ'):
                if a in words:
                    self.g54[i] = words[a] if L == 2 else self.m[i] - words[a]
            return self.ok()
        if 4 in g:
            self.t += words.get('P', 0)
            self._events()
            return self.ok()
        if 38.3 in g or 38.2 in g:
            return self._probe(words)
        if 53 in g:
            tgt = list(self.m)
            for i, a in enumerate('XYZ'):
                if a in words:
                    tgt[i] = words[a]
            self._move(tgt, rapid=True)
            return self.ok()
        if any(a in words for a in 'XYZ') and (0 in g or 1 in g or not g or set(g) <= {0, 1, 90, 91}):
            rapid = 0 in g
            tgt = list(self.m)
            for i, a in enumerate('XYZ'):
                if a in words:
                    tgt[i] = (words[a] + self.g54[i]) if self.abs else (self.m[i] + words[a])
            self._move(tgt, rapid=rapid)
        return self.ok()

    def _move(self, tgt, rapid):
        d = math.dist(self.m, tgt)
        f = 1500.0 if rapid else self.feed
        self.t += d / f * 60 + (0.05 if rapid else 0.0)
        if self.tool_off is not None:
            self.m = tgt
            self._update_phys()
            w = self.phys_work() + [self.work()[2]]
            s = self.surface(w[0], w[1])
            if s is not None and self.tip() < s - 1e-6:
                if rapid:
                    self.rapid_crash += 1
                else:
                    if not self.spindle:
                        self.cut_while_stopped += 1
                    self.cuts[self.tool].append((w[0], w[1], s - self.tip(), self.t))
        else:
            self.m = tgt
            self._update_phys()
        self._events()

    def _probe(self, words):
        w = self.work()
        s = self.surface(w[0], w[1])
        if self.tool_off is None:
            raise RuntimeError('probe with no tool mounted')
        if (s is not None and self.circuit and self.tip() <= s) or \
                (self.noise_status and self.rng.random() < self.noise_status):
            self.alarm = True
            self.noise_count += 1
            self.out.append('ALARM:4')
            self.ok()
            return
        z_tgt = (words['Z'] + self.g54[2]) if self.abs else self.m[2] + words['Z']
        contact_mz = (s - self.tool_off) if (s is not None and self.circuit) else None
        if contact_mz is not None and self.noise_early and self.rng.random() < self.noise_early:
            early = contact_mz + self.rng.uniform(0.03, 0.4)
            if early < self.m[2]:
                contact_mz = early
                self.noise_count += 1
        if contact_mz is not None and contact_mz >= z_tgt:
            self.t += abs(self.m[2] - contact_mz) / words.get('F', self.feed) * 60
            self.m[2] = contact_mz
            self.prb = (self.m[0], self.m[1], contact_mz, 1)
        else:
            self.t += abs(self.m[2] - z_tgt) / words.get('F', self.feed) * 60
            self.m[2] = z_tgt
            self.prb = (self.m[0], self.m[1], z_tgt, 0)
        if 'F' in words:
            self.feed = words['F']
        self._events()
        self.out.append(f'[PRB:{self.prb[0]:.3f},{self.prb[1]:.3f},{self.prb[2]:.3f}:{self.prb[3]}]')
        self.ok()


class SimUI:
    """Answers every prompt; mounts the requested tool and fakes the circuit-test touch."""
    def __init__(self, fake, quiet=False):
        self.fake, self.quiet, self.lines = fake, quiet, []

    def ask(self, msg):
        self.lines.append('ASK  ' + msg)
        if 'TOOL 1' in msg:
            self.fake.mount(1)
        elif 'TOOL 2' in msg:
            self.fake.mount(2)
        if 'CIRCUIT' in msg:
            self.fake.touch_pulse = 6

    def info(self, msg):
        self.lines.append('     ' + msg)
        if not self.quiet:
            print('     ' + msg)

    def warn(self, msg):
        self.lines.append('WARN ' + msg)
        if not self.quiet:
            print('WARN ' + msg)
