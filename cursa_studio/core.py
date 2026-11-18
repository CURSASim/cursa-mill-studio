"""CURSA Mill Studio core: GRBL 1.1 streaming, G38.3 probing, height-map levelling, backlash compensation.

Also a command-line runner (no GUI):
  python -m cursa_studio.core job traces.nc holes.nc [cutout.nc] [--backlash-x 0.1 --backlash-y 0.1] [--port COM5]
  python -m cursa_studio.core map            # height map test only, nothing is cut
  python -m cursa_studio.core probe / apply / rezero   # write *_leveled.nc files for another sender
"""
import argparse, json, math, os, queue, re, sys, threading, time
import numpy as np


RX_BUFFER = 127


class GrblError(RuntimeError):
    pass


class SerialTransport:
    def __init__(self, port, baud):
        import serial
        from serial.tools import list_ports
        if port == 'auto':
            port = self.find_grbl(serial, list_ports)
        self.port = port
        self.s = serial.Serial(port, baud, timeout=0.1)
        self.q = queue.Queue()
        self._run = True
        self._wlock = threading.Lock()
        threading.Thread(target=self._reader, daemon=True).start()

    @staticmethod
    def find_grbl(serial, list_ports):
        """Try every serial port (CH340 first) and keep the one that answers with a GRBL banner."""
        ports = sorted(list_ports.comports(), key=lambda p: 'CH340' not in (p.description or ''))
        tried = []
        for p in ports:
            tried.append(f'{p.device} ({p.description})')
            try:
                s = serial.Serial(p.device, 115200, timeout=0.2)
            except Exception:
                continue                       # busy (Candle?) or not a real port
            try:
                t0, buf = time.time(), b''
                s.write(b'\x18')
                while time.time() - t0 < 3.5:
                    buf += s.read(256)
                    if b'Grbl' in buf:
                        return p.device
                    if time.time() - t0 > 1.5 and not buf:
                        s.write(b'\x18')
            finally:
                s.close()
        raise GrblError('no GRBL machine found. Close Candle, check the USB cable and power. Ports seen: '
                        + (', '.join(tried) or 'none'))

    def _reader(self):
        buf = b''
        while self._run:
            buf += self.s.read(256)
            while b'\n' in buf:
                line, buf = buf.split(b'\n', 1)
                self.q.put(line.decode(errors='replace').strip())

    def write(self, data: bytes):
        with self._wlock:                 # GUI '?' polls and streamed lines come from different threads
            self.s.write(data)

    def readline(self, timeout):
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        self._run = False
        self.s.close()


class Grbl:
    def __init__(self, transport, log=print):
        self.t = transport
        self.log = log
        self.pending = []          # lengths of lines sent and not yet acknowledged
        self.status = {}
        self.prb = None
        self.alarm = None
        self.wco = (0.0, 0.0, 0.0)
        self.last_settings, self.last_offsets = {}, {}
        self.sim_clock = getattr(transport, 'clock', None)

    # ------------------------------------------------------------------ incoming
    def _handle(self, line):
        if not line:
            return
        if line == 'ok' or line.startswith('error'):
            if self.pending:
                self.pending.pop(0)
            if line.startswith('error'):
                raise GrblError(f'GRBL {line}')
        elif line.startswith('<'):
            self._parse_status(line)
        elif line.startswith('[PRB:'):
            m = re.match(r'\[PRB:([-\d.]+),([-\d.]+),([-\d.]+):(\d)\]', line)
            self.prb = (float(m[1]), float(m[2]), float(m[3]), m[4] == '1')
        elif line.startswith('ALARM'):
            self.alarm = line
            raise GrblError(f'GRBL {line}')
        elif line.startswith('Grbl'):
            self.pending.clear()
        elif line.startswith('$') and '=' in line:
            k, v = line.split('=', 1)
            self.last_settings[k] = v.split(' ')[0]
        elif re.match(r'\[(G5[4-9]|G28|G30|G92|TLO):', line):
            k, v = line[1:-1].split(':', 1)
            self.last_offsets[k] = v

    def _parse_status(self, line):
        body = line.strip('<>').split('|')
        st = {'state': body[0]}
        for f in body[1:]:
            k, _, v = f.partition(':')
            st[k] = v
        if 'WCO' in st:
            self.wco = tuple(float(a) for a in st['WCO'].split(','))
        if 'MPos' in st:
            m = tuple(float(a) for a in st['MPos'].split(','))
            st['mpos'] = m
            st['wpos'] = tuple(a - b for a, b in zip(m, self.wco))
        elif 'WPos' in st:
            w = tuple(float(a) for a in st['WPos'].split(','))
            st['wpos'] = w
            st['mpos'] = tuple(a + b for a, b in zip(w, self.wco))
        st['probe'] = 'P' in st.get('Pn', '')
        self.status = st

    def pump(self, timeout=0.05):
        line = self.t.readline(timeout)
        while line is not None:
            self._handle(line)
            line = self.t.readline(0)

    # ------------------------------------------------------------------ outgoing
    def reset(self):
        self.t.write(b'\x18')
        t0 = time.time()
        while time.time() - t0 < 5:
            line = self.t.readline(0.2)
            if line and line.startswith('Grbl'):
                self.pending.clear()
                self.alarm = None
                return line
        raise GrblError('no GRBL banner after reset — wrong port or baud?')

    def realtime(self, ch: bytes):
        self.t.write(ch)

    def query(self):
        self.t.write(b'?')
        t0 = time.time()
        self.status = {}
        while not self.status and time.time() - t0 < 2:
            self.pump(0.05)
        if not self.status:
            raise GrblError('no status reply')
        return self.status

    motion_filter = None             # e.g. Backlash(): may expand one line into several

    def send(self, line):
        """Character-counting stream: blocks only while GRBL's RX buffer is full."""
        line = line.strip()
        if not line:
            return
        if self.motion_filter is not None:
            out = self.motion_filter(line)
            if out != [line]:
                for l in out:
                    self._send_raw(l)
                return
        self._send_raw(line)

    def _send_raw(self, line):
        n = len(line) + 1
        while sum(self.pending) + n > RX_BUFFER:
            self.pump(0.05)
        self.pending.append(n)
        self.t.write((line + '\n').encode())
        self.pump(0)

    def cmd(self, line, timeout=30):
        self.send(line)
        t0 = time.time()
        while self.pending:
            self.pump(0.05)
            if time.time() - t0 > timeout:
                raise GrblError(f'timeout waiting for ok: {line}')

    def wait_idle(self, timeout=3600):
        t0 = time.time()
        while True:
            while self.pending:
                self.pump(0.05)
            st = self.query()
            if st['state'].startswith('Idle'):
                return st
            if st['state'].startswith('Alarm'):
                raise GrblError('GRBL in alarm')
            if time.time() - t0 > timeout:
                raise GrblError('timeout waiting for idle')
            if self.sim_clock is None:
                time.sleep(0.15)

    def settings(self):
        self.last_settings = {}
        self.cmd('$$')
        return dict(self.last_settings)

    def offsets(self):
        self.last_offsets = {}
        self.cmd('$#')
        return dict(self.last_offsets)

    noise_events = 0

    def wait_for_open_probe(self, settle=1.5):
        """The 3018 probe input picks up noise. A single 'closed' reading is not a short: wait up to `settle` s for
        the input to read open on 3 polls in a row; only a probe that STAYS closed is an error."""
        st = self.wait_idle()
        if not st['probe']:
            return
        self.noise_events += 1
        t0, open_run = time.time(), 0
        while time.time() - t0 < settle:
            if self.sim_clock is None:
                time.sleep(0.05)
            open_run = 0 if self.query()['probe'] else open_run + 1
            if open_run >= 3:
                return
        raise GrblError('probe input stays CLOSED before probing: the bit is touching the copper, the two clips '
                        'touch each other, or the probe wire is shorted')

    def recover_probe_alarm(self):
        """After ALARM:4 (probe read closed at the start of G38.x, usually a noise spike): collect the trailing
        replies, unlock and continue. Position is kept by GRBL for this alarm."""
        t0 = time.time()
        while time.time() - t0 < 0.6:
            try:
                self.pump(0.05)
            except GrblError:
                pass
        self.pending.clear()
        self.alarm = None
        self.noise_events += 1
        self.cmd('$X')
        self.cmd('G90')

    def probe(self, z_target_work, feed):
        """G38.3 to a work Z. Returns the contact MACHINE Z (GRBL reports PRB in machine coordinates) or None.
        G38.3 never alarms on a miss."""
        self.wait_for_open_probe()
        self.prb = None
        self.cmd(f'G38.3 Z{z_target_work:.4f} F{feed:g}', timeout=120)
        self.wait_idle()
        t0 = time.time()
        while self.prb is None and time.time() - t0 < 2:
            self.pump(0.05)
        if self.prb is None:
            raise GrblError('no PRB report')
        x, y, z, ok = self.prb
        return z if ok else None


# ---------------- backlash compensation


_W = re.compile(r'([XYZF])(-?\d+(?:\.\d*)?)')


class Backlash:
    def __init__(self, bx=0.0, by=0.0):
        self.b = {'X': float(bx), 'Y': float(by)}
        self.dir = {'X': 0, 'Y': 0}
        self.last = {'X': None, 'Y': None}     # last commanded (uncompensated) coordinate
        self.absolute = True

    @property
    def active(self):
        return any(v > 0 for v in self.b.values())

    def __call__(self, line):
        """Return the list of lines to send instead of `line`."""
        u = line.upper()
        if 'G91' in u:
            self.absolute = False
        if 'G90' in u:
            self.absolute = True
        if not self.active or not self.absolute or any(k in u for k in ('G53', 'G10', 'G38', 'G92', '$')):
            return [line]
        words = dict((k, float(v)) for k, v in _W.findall(u))
        if not ('X' in words or 'Y' in words):
            return [line]
        motion = 'G0' if re.match(r'\s*G0(?!\d)', u) else 'G1'
        pre = []
        out = {}
        for ax in ('X', 'Y'):
            if ax not in words:
                continue
            t, b = words[ax], self.b[ax]
            if self.last[ax] is not None and abs(t - self.last[ax]) > 1e-6:
                d = 1 if t > self.last[ax] else -1
                if self.dir[ax] != 0 and d != self.dir[ax] and b > 0:
                    # take up the slack at the reversal point before the move
                    pre.append(f'{motion} {ax}{self.last[ax] + d * b / 2:.4f}')
                self.dir[ax] = d
            self.last[ax] = t
            out[ax] = t + self.dir[ax] * b / 2
        new = line
        for ax, v in out.items():
            new = re.sub(rf'{ax}-?\d+(?:\.\d*)?', f'{ax}{v:.4f}', new, count=1)
        return pre + [new]


# ---------------- height map
class HeightMap:
    def __init__(self, xs, ys, z):
        self.xs, self.ys, self.z = np.asarray(xs, float), np.asarray(ys, float), np.asarray(z, float)

    def __call__(self, x, y):
        xs, ys, z = self.xs, self.ys, self.z
        x = min(max(x, xs[0]), xs[-1])
        y = min(max(y, ys[0]), ys[-1])
        i = min(max(np.searchsorted(xs, x) - 1, 0), len(xs) - 2)
        j = min(max(np.searchsorted(ys, y) - 1, 0), len(ys) - 2)
        tx = (x - xs[i]) / (xs[i + 1] - xs[i])
        ty = (y - ys[j]) / (ys[j + 1] - ys[j])
        return float(z[i, j] * (1 - tx) * (1 - ty) + z[i + 1, j] * tx * (1 - ty) +
                     z[i, j + 1] * (1 - tx) * ty + z[i + 1, j + 1] * tx * ty)

    @property
    def range(self):
        return float(self.z.max() - self.z.min())

    def to_dict(self):
        return {'x': self.xs.tolist(), 'y': self.ys.tolist(), 'z': self.z.round(4).tolist()}

    @classmethod
    def from_dict(cls, d):
        return cls(d['x'], d['y'], d['z'])





W = re.compile(r'([A-Z])\s*(-?\d*\.?\d+)')


# ------------------------------------------------------------------ G-code reading
def strip_comment(line):
    """Remove ( ) comments - nested ones too, and a stray ')' - and everything after ';'."""
    out, depth = [], 0
    for ch in line.split(';', 1)[0] if '(' not in line else line:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return ''.join(out).split(';', 1)[0].strip()


_WORDS = re.compile(r'^(\s*[A-Za-z]\s*[-+]?(\d+\.?\d*|\.\d+))+\s*$')


def check_file(path):
    """Lines GRBL would reject (no letter/number words after removing comments). Returns [(line_no, text)]."""
    bad = []
    for n, raw in enumerate(open(path, errors='replace'), 1):
        t = strip_comment(raw.strip())
        if not t or t == '%' or t.startswith('$'):
            continue
        if not _WORDS.match(t):
            bad.append((n, raw.strip()[:80]))
    return bad


def arc_points(x0, y0, x1, y1, i, j, cw, seg=0.25):
    cx, cy = x0 + i, y0 + j
    r = math.hypot(x0 - cx, y0 - cy)
    a0, a1 = math.atan2(y0 - cy, x0 - cx), math.atan2(y1 - cy, x1 - cx)
    if cw:
        while a1 >= a0 - 1e-9: a1 -= 2 * math.pi
    else:
        while a1 <= a0 + 1e-9: a1 += 2 * math.pi
    n = max(2, int(math.ceil(abs(a1 - a0) * r / seg)))
    return [(cx + r * math.cos(a0 + (a1 - a0) * k / n), cy + r * math.sin(a0 + (a1 - a0) * k / n)) for k in range(1, n + 1)]


def moves(path):
    """Yield (kind, line, (x0,y0,z0), (x1,y1,z1), words, motion). kind = 'move' | 'other'. Absolute mode only."""
    x = y = z = None
    motion, absolute = 0, True
    for raw in open(path, errors='replace'):
        line = strip_comment(raw.strip())
        if not line:
            continue
        u = line.upper()
        words = [(k, float(v)) for k, v in W.findall(u)]
        g = [v for k, v in words if k == 'G']
        if 91 in g:
            absolute = False
        if 90 in g:
            absolute = True
        if any(v in (53, 28, 30, 92, 10, 38.2, 38.3, 38.4, 38.5) for v in g):
            yield 'other', line, None, None, dict(words), motion
            continue
        for v in g:
            if v in (0, 1, 2, 3):
                motion = int(v)
        d = {k: v for k, v in words if k in 'XYZIJF'}
        if any(k in d for k in 'XYZ'):
            if not absolute:
                raise ValueError(f'{path}: relative moves (G91) are not supported: {line}')
            nx, ny, nz = d.get('X', x), d.get('Y', y), d.get('Z', z)
            yield 'move', line, (x, y, z), (nx, ny, nz), d, motion
            x, y, z = nx, ny, nz
        else:
            yield 'other', line, None, None, dict(words), motion


def cut_bounds(paths):
    xs, ys = [], []
    for p in paths:
        for kind, line, a, b, d, m in moves(p):
            if kind != 'move' or b[2] is None or b[2] >= 0 or None in b[:2]:
                continue
            xs.append(b[0]); ys.append(b[1])
            if a[0] is not None and a[1] is not None and a[2] is not None and a[2] < 0:
                xs.append(a[0]); ys.append(a[1])
            if m in (2, 3) and a[0] is not None and a[1] is not None:   # arcs bulge past their end points
                for px, py in arc_points(a[0], a[1], b[0], b[1], d.get('I', 0.0), d.get('J', 0.0), m == 2):
                    xs.append(px); ys.append(py)
    if not xs:
        raise ValueError('no cutting moves (Z < 0) found')
    return min(xs), min(ys), max(xs), max(ys)


# ------------------------------------------------------------------ G-code levelling
def level_file(src, hm, dst, seg=1.0):
    out = [f'(leveled by cursa_mill.autolevel from {os.path.basename(src)} - do not re-zero Z)']
    nmoves = 0
    for kind, line, a, b, d, motion in moves(src):
        if kind == 'other':
            out.append(line)
            continue
        x0, y0, z0 = a
        x1, y1, z1 = b
        feed = f" F{d['F']:g}" if 'F' in d else ''
        if None in (x1, y1, z1):          # position not fully known yet: pass through unchanged
            out.append(line)
            continue
        if motion in (2, 3) and x0 is not None:
            pts = arc_points(x0, y0, x1, y1, d.get('I', 0.0), d.get('J', 0.0), motion == 2)
            zs = [z0 + (z1 - z0) * (k + 1) / len(pts) for k in range(len(pts))]
            for k, ((px, py), pz) in enumerate(zip(pts, zs)):
                out.append(f'G1 X{px:.4f} Y{py:.4f} Z{pz + hm(px, py):.4f}' + (feed if k == 0 else ''))
            nmoves += 1
            continue
        g = 'G0' if motion == 0 else 'G1'
        if motion == 1 and x0 is not None and y0 is not None and z0 is not None:
            L = math.hypot(x1 - x0, y1 - y0)
            n = max(1, int(math.ceil(L / seg)))
            for k in range(1, n + 1):
                t = k / n
                px, py, pz = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, z0 + (z1 - z0) * t
                out.append(f'G1 X{px:.4f} Y{py:.4f} Z{pz + hm(px, py):.4f}' + (feed if k == 1 else ''))
        else:
            # rapids and the first move: keep the original axes, correct Z at the end point
            parts = [g]
            if 'X' in d: parts.append(f'X{x1:.4f}')
            if 'Y' in d: parts.append(f'Y{y1:.4f}')
            parts.append(f'Z{z1 + hm(x1, y1):.4f}')
            out.append(' '.join(parts) + feed)
        nmoves += 1
    with open(dst, 'w', newline='\r\n') as f:
        f.write('\n'.join(out) + '\n')
    return nmoves


# ------------------------------------------------------------------ probing
class Leveler:
    def __init__(self, g, ui, feed_seek=60, feed_map=30, feed_fine=10):
        self.g, self.ui = g, ui
        self.z0 = None
        self.fs, self.fm, self.ff = feed_seek, feed_map, feed_fine

    def connect(self):
        print('connected:', self.g.reset())
        st = self.g.query()
        if st['state'].startswith('Alarm'):
            self.g.cmd('$X')
        self.g.cmd('G21 G90 G94 G17')
        self.g.cmd('G92.1')
        self.g.cmd('M5')
        if self.g.query()['probe']:
            raise GrblError('probe input reads CLOSED with nothing touching: the clip leads are shorted together')

    def circuit_test(self):
        self.ui.ask('CIRCUIT TEST: copper clip on the board, the other probe lead FREE. Press Enter, then touch the '
                    'free lead to the copper clip for a second.')
        t0, seen = time.time(), False
        while time.time() - t0 < 60:
            p = self.g.query()['probe']
            seen |= p
            if seen and not p:
                print('circuit test OK')
                self.ui.confirm('Circuit OK. Now clip the free probe lead onto the BIT (bit 5-15 mm above the copper). '
                                'The machine moves down to touch the copper as soon as you confirm.')
                return
            if self.g.sim_clock is None:
                time.sleep(0.05)
        raise GrblError('circuit test failed: the probe input never closed — check both clips and the probe plug')

    def _rel_probe(self, dist, feed):
        """Relative G38.3 down by `dist`. Returns contact machine Z or None (no contact). Retries noise alarms."""
        for attempt in range(4):
            try:
                self.g.wait_for_open_probe()
                self.g.prb = None
                self.g.cmd('G91')
                self.g.cmd(f'G38.3 Z-{dist:.3f} F{feed:g}', timeout=dist / feed * 60 + 60)
                self.g.cmd('G90')
                self.g.wait_idle()
                t0 = time.time()
                while self.g.prb is None and time.time() - t0 < 2:
                    self.g.pump(0.05)
                if not self.g.prb:
                    raise GrblError('no PRB report')
                return self.g.prb[2] if self.g.prb[3] else None
            except GrblError as e:
                m = str(e)
                if not ('ALARM:4' in m or ('in alarm' in m and self.g.alarm in (None, 'ALARM:4'))) or attempt == 3:
                    raise
                print('  probe noise (ALARM:4) - unlocking and retrying')
                self.g.recover_probe_alarm()
                self.g.cmd('G90')

    def touch_blind(self, x, y, max_drop=25.0):
        """From the current (unknown) height: move over (x, y), seek down at most max_drop, then fine touches until
        the lowest reading is confirmed twice (noise reads high). Returns machine Z of the copper."""
        self.g.cmd(f'G0 X{x:.3f} Y{y:.3f}')
        st = self.g.wait_idle()
        start = st['mpos'][2]
        vals = []
        for _ in range(10):
            if not vals:
                left = max_drop - (start - self.g.status.get('mpos', (0, 0, start))[2])
                if left <= 0.05:
                    break
                zc = self._rel_probe(left, self.fs)               # fast seek
                if zc is None:
                    break
                self.g.cmd('G91'); self.g.cmd('G0 Z0.3'); self.g.cmd('G90')
                zf = self._rel_probe(1.0, self.ff)                # fine touch below the seek contact
                if zf is None:                                    # seek stopped early (noise): keep seeking
                    continue
                vals.append(zf)
            else:
                self.g.cmd('G91'); self.g.cmd('G0 Z0.2'); self.g.cmd('G90')
                zf = self._rel_probe(1.0, self.ff)
                if zf is None:
                    raise GrblError('lost the copper during the Z0 confirm touch: check the clips')
                vals.append(zf)
            c = self.confirmed_low(vals)
            if c is not None:
                self.g.cmd('G91'); self.g.cmd('G0 Z1'); self.g.cmd('G90')
                return c
        if vals:
            raise GrblError(f'Z0 touches disagree ({", ".join(f"{v:.3f}" for v in vals)}): the probe signal is noisy')
        raise GrblError(f'no copper within {max_drop:g} mm below: clip off, bit too high, or not over the board')

    fine_feed = 20.0
    agree = 0.015            # two fine touches must agree within this (mm)

    last_map = None

    def accept_big_range(self, hm, limit):
        """Called when the copper is more than `limit` out of flat. Return True to use the map anyway (the GUI asks
        the operator); the console runner stops."""
        return False

    def confirmed_low(self, vals):
        """Noise can only make a touch read HIGH (the probe stops early), never low. So the copper is the LOWEST
        reading, accepted once a second reading agrees with it. Returns that value or None."""
        lo = min(vals)
        near = [v for v in vals if v - lo <= self.agree]
        return sum(near) / len(near) if len(near) >= 2 else None

    def _probe_retry(self, target_work, feed):
        for attempt in range(4):
            try:
                return self.g.probe(target_work, feed)
            except GrblError as e:
                m = str(e)
                probe_alarm = 'ALARM:4' in m or ('in alarm' in m and self.g.alarm in (None, 'ALARM:4'))
                if not probe_alarm or attempt == 3:
                    raise
                print('  probe noise (ALARM:4) - unlocking and retrying')
                self.g.recover_probe_alarm()

    def touch(self, x, y, z_est, travel):
        """Fast touch to find the copper, then fine touches from 0.2 mm above until two agree. A noise spike makes
        a touch read HIGH (early trigger), so a single reading is never trusted."""
        self.g.cmd(f'G0 Z{travel:.3f}')
        self.g.cmd(f'G0 X{x:.3f} Y{y:.3f}')
        for below in (1.0, 3.0):
            self.g.cmd(f'G0 Z{z_est + 1.0:.3f}')
            target = z_est - below
            zm = self._probe_retry(target, self.fm)
            if zm is None:
                continue
            vals = [zm]                    # the fast touch is reading 1; one agreeing fine touch confirms it
            for _ in range(7):
                self.g.cmd(f'G0 Z{min(vals) - self.z0 + 0.2:.4f}')
                zf = self._probe_retry(target, self.fine_feed)
                if zf is None:
                    break
                vals.append(zf)
                c = self.confirmed_low(vals)
                if c is not None:
                    z = c - self.z0
                    self.g.cmd(f'G0 Z{z + 1.0:.3f}')
                    return z
            if vals:
                raise GrblError(f'probe readings disagree at X{x:.1f} Y{y:.1f} ({", ".join(f"{v - self.z0:.3f}" for v in vals)}): '
                                'the probe signal is noisy. Shorten/twist the probe wires, keep them away from the '
                                'stepper cables, add a 100 nF capacitor across the probe input')
        raise GrblError(f'no copper at X{x:.1f} Y{y:.1f} — clip off or the point is off the copper')

    def zero_xy(self):
        """X/Y zero. Candle zeroes with G92, which GRBL loses on reset/reconnect, so zero here into G54 instead."""
        here = self.ui.yesno('Is the bit RIGHT ABOVE the blank lower-left corner (the X/Y zero point)?  '
                             'y = zero X/Y here,  n = keep the X/Y zero saved from last time')
        if here:
            self.g.cmd('G10 L20 P1 X0 Y0')
            print('X/Y zero set here (saved in G54).')
        else:
            print('keeping the saved G54 X/Y zero')

    def set_z0_here(self, zc_machine, z_work=0.0):
        self.z0 = zc_machine - z_work
        self.g.cmd(f'G10 L2 P1 Z{self.z0:.4f}')

    def grid(self, bounds, spacing=10.0, residual_max=0.04, range_max=1.0, range_warn=0.4, refine=False):
        x0, y0, x1, y1 = bounds
        nx = max(2, math.ceil((x1 - x0) / spacing) + 1)
        ny = max(2, math.ceil((y1 - y0) / spacing) + 1)
        xs = list(np.linspace(x0, x1, nx)); ys = list(np.linspace(y0, y1, ny))
        meas = {}
        top = 0.0

        def get(x, y):
            nonlocal top
            key = (round(x, 4), round(y, 4))
            if key in meas:
                return meas[key]
            near = min(meas, key=lambda q: math.dist(q, key)) if meas else None
            z = self.touch(x, y, meas[near] if near else 0.0, top + 1.5)
            meas[key] = z
            top = max(top, z)
            return z

        def build(xs, ys):
            Z = np.zeros((len(xs), len(ys)))
            for i, x in enumerate(xs):
                for j in (range(len(ys)) if i % 2 == 0 else range(len(ys) - 1, -1, -1)):
                    Z[i, j] = get(x, ys[j])
                print(f'  map column {i + 1}/{len(xs)}')
            return HeightMap(xs, ys, Z)

        print(f'probing {nx} x {ny} = {nx * ny} points over X {x0:.1f}..{x1:.1f}, Y {y0:.1f}..{y1:.1f}')
        hm = build(xs, ys)
        self.last_map = hm                    # kept even if we stop below
        if hm.range > range_max and not self.accept_big_range(hm, range_max):
            raise GrblError(f'height range {hm.range:.2f} mm > {range_max}: the blank is not seated (lift it, clean '
                            'under it, re-clamp) or badly bowed')
        if not refine:                        # default: the grid alone (Ale 17:26: "shouldn't need more than those
            print(f'map range {hm.range:.3f} mm')  # 80 points"); a 10 mm grid on a smooth bow is well inside 0.04
            if hm.range > range_warn:
                print(f'NOTE: the copper is {hm.range:.2f} mm out of flat: compensated, check the blank is clamped down')
            self.g.cmd(f'G0 Z{top + 5:.3f}')
            return hm
        # optional: spot-check cell centres; refine ONLY the columns / rows whose cells miss. 40 um on a 0.08 deep V cut changes
        # the groove width by ~0.01 mm, so a smaller miss is not worth the probing time.
        ci = sorted(set(list(range(0, nx - 1, max(1, (nx - 1) // 3))) + [nx - 2]))
        cj = sorted(set(list(range(0, ny - 1, max(1, (ny - 1) // 2))) + [ny - 2]))
        miss = {}
        for i in ci:
            for j in cj:
                x, y = (xs[i] + xs[i + 1]) / 2, (ys[j] + ys[j + 1]) / 2
                miss[(i, j)] = abs(get(x, y) - hm(x, y))
        res = max(miss.values())
        print(f'map range {hm.range:.3f} mm, worst cell-centre error {res * 1000:.0f} um')
        if hm.range > range_warn:
            print(f'NOTE: the copper is {hm.range:.2f} mm out of flat: compensated, but check the blank is clamped down')
        bad = [k for k, v in miss.items() if v > residual_max]
        if bad:
            bi = {i for i, _ in bad}
            bj = {j for _, j in bad}
            xs2 = sorted(set(xs + [(xs[i] + xs[i + 1]) / 2 for i in bi]))
            ys2 = sorted(set(ys + [(ys[j] + ys[j + 1]) / 2 for j in bj]))
            print(f'refining {len(bi)} column(s) / {len(bj)} row(s): {len(xs2)} x {len(ys2)}')
            hm = build(xs2, ys2)
        self.g.cmd(f'G0 Z{top + 5:.3f}')
        return hm


class ConsoleUI:
    def ask(self, msg):
        print('\n>>> ' + msg)
        input('    [Enter] ')

    def confirm(self, msg):
        """Dual confirm: Enter, then type GO. Anything else aborts."""
        print('\n' + '=' * 70 + '\n>>> ' + msg)
        input('    [Enter] to continue ')
        if input('    Type GO to confirm: ').strip().lower() != 'go':
            raise KeyboardInterrupt
        return True

    def yesno(self, msg):
        return input('\n>>> ' + msg + ' [y/n] ').strip().lower().startswith('y')


def leveled_lines(path, hm, seg=1.0):
    """Level a file in memory for streaming: drop comments, M2/M30 and %; keep everything else."""
    tmp = path + '.~lev'
    level_file(path, hm, tmp, seg)
    out = []
    for line in open(tmp):
        line = strip_comment(line.strip())
        if not line or line == '%' or re.match(r'^M0?(2|30)\b', line.upper()):
            continue
        out.append(line)
    os.remove(tmp)
    return out


def stream(g, lines, label):
    n = len(lines)
    t0 = time.time()
    for k, line in enumerate(lines):
        g.send(line)
        if k % 200 == 0 and n:
            print(f'\r  {label}: {100 * k / n:5.1f} %  ({time.time() - t0:5.0f} s)', end='', flush=True)
    g.wait_idle()
    print(f'\r  {label}: 100.0 %  ({time.time() - t0:5.0f} s)')


def _save(hm, ref, path):
    json.dump({'format': 'cursa-heightmap/1', 'ref': ref, 'z_ref': 0.0, 'map': hm.to_dict(),
               'note': 'work Z0 = copper at ref, set by the probing tool'}, open(path, 'w'), indent=1)
    with open(path.replace('.json', '.csv'), 'w') as f:
        f.write('x,y,z\n' + ''.join(f'{x:.3f},{y:.3f},{hm.z[i, j]:.4f}\n'
                                    for i, x in enumerate(hm.xs) for j, y in enumerate(hm.ys)))


def report(hm, path):
    """Print the map as a table (µm, relative to the lowest point) and save a colour PNG next to the JSON."""
    z = hm.z - hm.z.min()
    print(f'\nheight map (um above the lowest point), range {hm.range * 1000:.0f} um; rows = Y (top = back)')
    print('   Y\\X ' + ''.join(f'{x:7.1f}' for x in hm.xs))
    for j in range(len(hm.ys) - 1, -1, -1):
        print(f'{hm.ys[j]:7.1f} ' + ''.join(f'{z[i, j] * 1000:7.0f}' for i in range(len(hm.xs))))
    verdict = ('GOOD (< 0.2 mm)' if hm.range < 0.2 else 'OK, compensated (0.2-0.6 mm)' if hm.range <= 0.6
               else 'TOO BOWED / NOT SEATED')
    print(f'verdict: {verdict}.  Saved {path} and .csv')
    try:
        import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(8, 5.5))
        im = ax.pcolormesh(hm.xs, hm.ys, z.T * 1000, shading='gouraud', cmap='viridis')
        ax.plot(*np.meshgrid(hm.xs, hm.ys), 'k+', ms=4)
        fig.colorbar(im, label='um above lowest point'); ax.set_aspect('equal')
        ax.set_title(f'copper height map, range {hm.range * 1000:.0f} um'); ax.set_xlabel('X mm'); ax.set_ylabel('Y mm')
        png = path.replace('.json', '.png'); fig.savefig(png, dpi=110); print('picture:', png)
    except Exception as e:
        print('(no picture:', e, ')')


def run_job(a, g, lv, ui):
    files = list(a.files) if a.no_sort else sorted(a.files, key=lambda f: os.path.basename(f).lower())
    for f in files:
        if not os.path.exists(f):
            raise GrblError(f'file not found: {f}')
    bx0, by0, bx1, by1 = cut_bounds(files)
    m = a.margin
    bounds = (bx0 - m, by0 - m, bx1 + m, by1 + m)
    ref = [bounds[0], bounds[1]]
    print('\nJOB ORDER:')
    for k, f in enumerate(files):
        print(f'  {k + 1}. {os.path.basename(f)}')
    print(f'probe area X {bounds[0]:.1f}..{bounds[2]:.1f}  Y {bounds[1]:.1f}..{bounds[3]:.1f}')
    ui.confirm('Check the order above. Blank locked in the fixture. Bit 10-20 mm above the copper.')
    bl = Backlash(a.backlash_x, a.backlash_y)
    hm = None
    for k, f in enumerate(files):
        name = os.path.basename(f)
        has_m3 = any(re.search(r'\bM0?3\b', strip_comment(l).upper()) for l in open(f, errors='replace'))
        change = (k == 0) or ui.yesno(f'Next file: {name}. Does it need a DIFFERENT bit than the one in now?')
        if change:
            g.cmd('M5')
            if getattr(lv, 'z0', None) is not None:      # only once Z is known; before that, never move Z blind
                g.cmd('G0 Z10')
            ui.confirm(f'BIT for {name}: put it in now (shank fully in, collet tight, stick-out >= 10 mm). Copper clip on '
                       'the board, other probe lead free for the circuit test, bit 10-20 mm above the copper.')
            lv.circuit_test()
            lv.set_z0_here(lv.touch_blind(*ref))
            print(f'Z0 set on the copper at X{ref[0]:.2f} Y{ref[1]:.2f} with this bit; mapping...')
            hm = lv.grid(bounds, a.grid)
            _save(hm, ref, os.path.splitext(f)[0] + '.heightmap.json')
            report(hm, os.path.splitext(f)[0] + '.heightmap.json')
            g.cmd(f'G0 Z{max(hm.z.max(), 0) + 5:.3f}')
            ui.confirm(f'TAKE THE CLIP OFF THE BIT. Spindle starts and {name} runs next.')
        else:
            ui.confirm(f'Same bit. {name} runs next (spindle starts).')
        lines = leveled_lines(f, hm)
        if not has_m3:
            lines = [f'M3 S{a.spindle:g}', 'G4 P3'] + lines
        g.motion_filter = bl if bl.active else None
        try:
            stream(g, lines, name)
        finally:
            g.motion_filter = None
        g.cmd('M5')
        g.cmd(f'G0 Z{max(hm.z.max(), 0) + 10:.3f}')
        print(f'finished {name}')
    ui.ask('JOB DONE. Spindle off. Snip the tabs, deburr, check every net with the meter.')
    return 0


def main(argv=None, transport=None, ui=None):
    ap = argparse.ArgumentParser(prog='cursa_job.py', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--port', default='auto')
    ap.add_argument('--baud', type=int, default=115200)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('probe'); p.add_argument('files', nargs='+')
    p.add_argument('--grid', type=float, default=10.0, help='max probe spacing, mm')
    p.add_argument('--margin', type=float, default=1.5, help='probe this far beyond the cutting moves, mm')
    p.add_argument('--name', default=None, help='height map name (default: first file name)')
    p.add_argument('--no-circuit-test', action='store_true')
    a2 = sub.add_parser('apply'); a2.add_argument('map'); a2.add_argument('files', nargs='+')
    r = sub.add_parser('rezero'); r.add_argument('map')
    j = sub.add_parser('job', help='run .nc files in order: probe+map, mill, bit change, re-probe, ...')
    j.add_argument('files', nargs='+')
    j.add_argument('--grid', type=float, default=10.0)
    j.add_argument('--margin', type=float, default=1.5)
    j.add_argument('--spindle', type=float, default=1000, help='S value if a file has no M3 of its own')
    j.add_argument('--backlash-x', type=float, default=0.0)
    j.add_argument('--backlash-y', type=float, default=0.0)
    j.add_argument('--no-sort', action='store_true', help='keep the given order instead of sorting by name')
    mp_ = sub.add_parser('map', help='test: probe a rectangle and report the height map (no G-code needed)')
    mp_.add_argument('--area', type=float, nargs=4, default=[5, 5, 95, 65], metavar=('X0', 'Y0', 'X1', 'Y1'))
    mp_.add_argument('--grid', type=float, default=10.0)
    mp_.add_argument('--name', default='test')
    mp_.add_argument('--no-circuit-test', action='store_true')
    a = ap.parse_args(argv)
    ui = ui or ConsoleUI()

    if a.cmd == 'apply':
        d = json.load(open(a.map)); hm = HeightMap.from_dict(d['map'])
        for f in a.files:
            dst = os.path.splitext(f)[0] + '_leveled.nc'
            print(f'{dst}: {level_file(f, hm, dst)} moves leveled')
        return 0

    g = Grbl(transport or SerialTransport(a.port, a.baud))
    lv = Leveler(g, ui)
    try:
        lv.connect()
        if a.cmd != 'rezero':
            lv.zero_xy()
        if a.cmd == 'rezero':
            d = json.load(open(a.map)); rx, ry = d['ref']
            ui.ask('REZERO after a bit change: ground clip on the NEW bit, raise it 10+ mm above the copper. Enter.')
            zc = lv.touch_blind(rx, ry)
            lv.set_z0_here(zc, d.get('z_ref', 0.0))
            g.cmd('G0 Z10')
            ui.ask('Z restored to the height map frame. TAKE THE CLIP OFF THE BIT, then run the next *_leveled.nc in '
                   'Candle (do not zero Z).')
            return 0
        if a.cmd == 'job':
            return run_job(a, g, lv, ui)
        if a.cmd == 'map':
            bounds = tuple(a.area)
            ui.ask(f'MAP TEST: copper clip on the board, ground '
                   f'clip on the bit, bit 10-20 mm above the copper. Area X {bounds[0]:g}..{bounds[2]:g}, Y '
                   f'{bounds[1]:g}..{bounds[3]:g}. Enter to start.')
            if not a.no_circuit_test:
                lv.circuit_test()
            ref = [bounds[0], bounds[1]]
            lv.set_z0_here(lv.touch_blind(*ref))
            hm = lv.grid(bounds, a.grid)
            path = os.path.abspath(a.name + '.heightmap.json')
            _save(hm, ref, path)
            report(hm, path)
            g.cmd('G0 Z10')
            ui.ask('Map test done. Take the clip off the bit.')
            return 0
        bx0, by0, bx1, by1 = cut_bounds(a.files)
        m = a.margin
        bounds = (bx0 - m, by0 - m, bx1 + m, by1 + m)
        ref = [bounds[0], bounds[1]]
        ui.ask(f'Copper clip on the board, ground clip on the bit. Jog the bit to '
               f'10-20 mm above the copper (clear of the fixture walls and cams). Probe area X {bounds[0]:.1f}..'
               f'{bounds[2]:.1f}, Y {bounds[1]:.1f}..{bounds[3]:.1f}. Enter to start.')
        if not a.no_circuit_test:
            lv.circuit_test()
        zc = lv.touch_blind(*ref)
        lv.set_z0_here(zc)
        print(f'Z0 set on the copper at X{ref[0]:.2f} Y{ref[1]:.2f}')
        hm = lv.grid(bounds, a.grid)
        name = a.name or os.path.splitext(os.path.basename(a.files[0]))[0]
        mp = os.path.join(os.path.dirname(os.path.abspath(a.files[0])), name + '.heightmap.json')
        _save(hm, ref, mp)
        print(f'height map saved: {mp}')
        for f in a.files:
            dst = os.path.splitext(f)[0] + '_leveled.nc'
            print(f'{dst}: {level_file(f, hm, dst)} moves leveled')
        g.cmd('G0 Z10')
        ui.ask('DONE. Take the clip off the bit. Close this window, open Candle, load the *_leveled.nc file and run it. '
               'Do NOT zero Z in Candle. After a bit change run:  python -m cursa_mill.autolevel rezero ' + mp)
        return 0
    except GrblError as e:
        print('\nSTOPPED:', e)
        try:
            g.realtime(b'!'); time.sleep(0.2); g.reset(); g.cmd('$X')
        except Exception:
            pass
        return 2
    except KeyboardInterrupt:
        g.realtime(b'!'); g.reset()
        print('\nSTOPPED (Ctrl+C)')
        return 2


if __name__ == '__main__':
    sys.exit(main())
