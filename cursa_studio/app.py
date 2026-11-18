"""CURSA Mill Studio — PCB milling control for the Genmitsu 3018-PROVer V2 (GRBL 1.1).

One window: connect, jog every axis with the keyboard, pick the traces / holes / cut-out files, then one guided job:
load carving bit + copper clip -> circuit check -> clip lead on the bit -> Z0 + height map -> remove clips -> mill traces
-> swap to drill -> circuit check -> clip lead on the bit -> Z0 + height map again -> remove clips -> drill holes
(+ cut-out). Every probing start and every spindle start is a dual confirm.

Keys: arrows = X / Y, Z = Z up, X = Z down (also PgUp / PgDn), SPACE = step 10 / 1 / 0.1 mm, ESC = STOP.
Machine control reuses cursa_job.py (character-counting streaming, G38.3 probing, height map, backlash compensation).
"""
import json, math, os, queue, re, sys, threading, time, traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from . import core as cj
from . import __version__

APP, VERSION = 'CURSA Mill Studio', __version__
def _data_dir():
    docs = os.path.join(os.path.expanduser('~'), 'Documents')
    d = os.path.join(docs if os.path.isdir(docs) else os.path.expanduser('~'), 'CURSA Mill Studio')
    os.makedirs(d, exist_ok=True)
    return d


HOME = _data_dir()                                    # Documents\CURSA Mill Studio (settings + height maps)
RUNS = os.path.join(HOME, 'runs')
SETTINGS = os.path.join(HOME, 'settings.json')
DEFAULTS = {'copper_w': 102.0, 'copper_h': 71.0, 'backlash_x': 0.0, 'backlash_y': 0.0, 'spindle': 1000.0,
            'grid': 10.0, 'margin': 1.5, 'jog_xy_feed': 1500.0, 'jog_z_feed': 500.0, 'last_dir': '',
            'traces': '', 'holes': '', 'cutout': '', 'shortcut_asked': False}
STEPS = (10.0, 1.0, 0.1)

ERR_HINT = {
    'error:9': 'machine is LOCKED (alarm): click Home, or Unlock',
    'error:15': 'jog would go past the machine travel limit',
    'error:8': 'GRBL is busy: wait until it is Idle',
    'ALARM:1': 'hard limit hit: release the switch by hand, Reset, then Home',
    'ALARM:2': 'move would exceed soft limits',
    'ALARM:3': 'reset while moving: position may be lost. Home again (or Unlock) and re-zero X/Y',
    'ALARM:4': 'probe read CLOSED at the start of a touch (usually a noise spike): retried automatically',
    'ALARM:6': 'homing failed: is the E-STOP pressed? Twist it out, Reset, Home',
    'ALARM:8': 'homing failed: switch not found',
    'ALARM:9': 'homing failed: switch not found',
    'Estop': 'E-STOP is pressed: twist the red button out, then Reset and Unlock/Home',
}


def hint(msg):
    for k, v in ERR_HINT.items():
        if k in msg:
            return f'{msg}  ->  {v}'
    return msg


def load_settings():
    s = dict(DEFAULTS)
    try:
        s.update(json.load(open(SETTINGS)))
    except Exception:
        pass
    return s


def save_settings(s):
    try:
        json.dump(s, open(SETTINGS, 'w'), indent=2)
    except Exception:
        pass


def make_shortcuts():
    """Windows: Desktop + Start Menu shortcuts that start the app with pythonw.exe (signed by the PSF, so Smart App
    Control allows it). Returns the paths written."""
    import subprocess
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), 'pythonw.exe')
    if os.path.exists(pyw):
        exe = pyw
    workdir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # folder that contains cursa_studio\
    ps = (
        "$w = New-Object -ComObject WScript.Shell; $out = @(); "
        "foreach ($f in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) { "
        "$p = Join-Path $f 'CURSA Mill Studio.lnk'; $s = $w.CreateShortcut($p); "
        f"$s.TargetPath = '{exe}'; $s.Arguments = '-m cursa_studio'; $s.WorkingDirectory = '{workdir}'; "
        "$s.Description = 'CURSA Mill Studio'; $s.Save(); $out += $p }; $out -join '|'")
    r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps], capture_output=True,
                       text=True, timeout=60, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or 'PowerShell failed')
    return [p for p in r.stdout.strip().split('|') if p]


def plan_steps(files):
    """files: dict role -> path. Returns [(group, key, text)] — shared by the GUI list and the machine thread."""
    steps = []
    if files.get('traces'):
        steps += [('carve', 'load', 'Load CARVING bit, copper clip on'), ('carve', 'circuit', 'Circuit check'),
                  ('carve', 'clipon', 'Clip probe lead on the bit'), ('carve', 'level', 'Find Z0 + height map'), ('carve', 'clips', 'Remove probe clips'),
                  ('carve', 'cut:traces', 'Mill TRACES')]
    if files.get('holes') or files.get('cutout'):
        first = not steps
        steps += [('drill', 'load', ('Load' if first else 'Swap to') + ' DRILL bit, copper clip on'),
                  ('drill', 'circuit', 'Circuit check'), ('drill', 'clipon', 'Clip probe lead on the bit'),
                  ('drill', 'level', ('Find' if first else 'Re-find') + ' Z0 + height map'),
                  ('drill', 'clips', 'Remove probe clips')]
        if files.get('holes'):
            steps.append(('drill', 'cut:holes', 'Drill HOLES'))
        if files.get('cutout'):
            steps.append(('drill', 'cut:cutout', 'CUT-OUT board'))
    steps.append(('end', 'done', 'Done'))
    return steps


def parse_preview(path):
    """Cutting polylines and plunge points of a G-code file (absolute, work coordinates)."""
    cuts, plunges = [], set()
    cur = []
    for kind, line, a, b, d, m in cj.moves(path):
        if kind != 'move' or a is None or None in a or None in b:
            if cur:
                cuts.append(cur); cur = []
            continue
        if b[2] < 0 and a[2] < 0 and (abs(a[0] - b[0]) > 1e-6 or abs(a[1] - b[1]) > 1e-6):
            if m in (2, 3):
                pts = cj.arc_points(a[0], a[1], b[0], b[1], d.get('I', 0.0), d.get('J', 0.0), m == 2)
            else:
                pts = [(b[0], b[1])]
            if not cur:
                cur = [(a[0], a[1])]
            cur += pts
        else:
            if cur:
                cuts.append(cur); cur = []
            if b[2] < 0 and abs(a[0] - b[0]) < 1e-6 and abs(a[1] - b[1]) < 1e-6 and b[2] < a[2]:
                plunges.add((round(b[0], 3), round(b[1], 3)))
    if cur:
        cuts.append(cur)
    return cuts, sorted(plunges)


# ======================================================================================== machine side
class Abort(Exception):
    pass


class GuiGrbl(cj.Grbl):
    on_line = None

    def _handle(self, line):
        if line and self.on_line and not line.startswith(('<', '[PRB:')) and line != 'ok':
            self.on_line(line)
        super()._handle(line)


def make_sim():
    from .sim import FakeGrbl

    class SimGrbl(FakeGrbl):
        """Simulated mill + warped copper blank (102 x 71) for trying the app without a machine."""
        def write(self, data):
            super().write(data.replace(b'\x85', b''))

        def readline(self, timeout):
            if not self.out:
                time.sleep(min(timeout, 0.01))
                return None
            return self.out.popleft()

        def _line(self, line):
            u = line.upper().strip()
            if u.startswith('$J='):
                body = re.sub(r'G9[01]|G21', '', u[3:])
                old, self.abs = self.abs, 'G91' not in u
                try:
                    return super()._line('G1 ' + body)
                finally:
                    self.abs = old
            if u.startswith('M5') or u.startswith('M3') or u.startswith('G92') or u.startswith('G4'):
                return super()._line(line)
            return super()._line(line)

        def close(self):
            pass

    nz = float(os.environ.get('CURSA_SIM_NOISE', '0') or 0)     # e.g. 0.15 to rehearse a noisy probe line
    s = SimGrbl(backlash=0.05, noise_early=nz, noise_status=nz / 5)
    s.mount(1)
    s.m = [-180.0, -120.0, -38.0 + 12.0]      # bit 12 mm above the copper corner, X/Y already zeroed there
    return s


class Machine(threading.Thread):
    def __init__(self, post):
        super().__init__(daemon=True)
        self.post = post
        self.q = queue.Queue()
        self.reply = queue.Queue()
        self.g = None
        self.t = None
        self.sim = None
        self.job_running = False
        self.waiting = False
        self.abort = False

    # ------------------------------------------------------------------ loop
    def run(self):
        while True:
            try:
                if self.g is None:
                    self.handle(self.q.get())
                    continue
                try:
                    item = self.q.get_nowait()
                except queue.Empty:
                    item = None
                if item:
                    self.handle(item)
                else:
                    self.g.pump(0.05)
            except cj.GrblError as e:
                self.post(('error', hint(str(e))))
            except Exception as e:
                self.post(('error', f'{type(e).__name__}: {e}'))
                self.post(('log', traceback.format_exc()))

    def service(self):
        """While a job waits for the operator: allow jogging and simple commands, keep reading GRBL."""
        try:
            item = self.q.get_nowait()
        except queue.Empty:
            item = None
        try:
            if item and item[0] in ('jog', 'gcode', 'zero_xy', 'spindle_off', 'unlock'):
                self.handle(item)
            elif item and item[0] in ('job', 'home', 'connect', 'disconnect', 'goto0'):
                self.post(('error', 'not while a job is running (STOP the job first)'))
            else:
                self.g.pump(0.05)
        except cj.GrblError as e:
            self.post(('error', hint(str(e))))

    def handle(self, item):
        kind = item[0]
        if kind == 'connect':
            return self.connect(item[1])
        if kind == 'disconnect':
            return self.disconnect()
        if self.g is None:
            return self.post(('error', 'not connected'))
        g = self.g
        if kind == 'jog':
            _, axis, d, f = item
            g.cmd(f'$J=G91 G21 {axis}{d:.3f} F{f:g}')
        elif kind == 'home':
            self.post(('log', 'Homing...'))
            g.cmd('$H', timeout=240)
            g.cmd('G21 G90')
            g.cmd('G92.1')
            self.post(('log', 'Homed.'))
        elif kind == 'unlock':
            g.cmd('$X')
            g.cmd('G21 G90')
            g.cmd('G92.1')
            self.post(('log', 'Unlocked. (Position is NOT referenced until you Home.)'))
        elif kind == 'reset':
            g.realtime(b'\x18')
            g.pending.clear()
            self.post(('log', 'Soft reset sent.'))
        elif kind == 'zero_xy':
            g.cmd('G92.1')
            g.cmd('G10 L20 P1 X0 Y0')
            self.post(('log', 'X/Y zero set at the current position (saved in G54).'))
        elif kind == 'goto0':
            g.cmd('G90 G0 X0 Y0')
        elif kind == 'spindle_off':
            g.cmd('M5')
        elif kind == 'gcode':
            g.cmd(item[1])
        elif kind == 'job':
            self.run_job(item[1])

    def connect(self, port):
        self.disconnect(quiet=True)
        try:
            if port == 'Simulator':
                t = make_sim()
                self.sim = t
            else:
                self.post(('log', 'Looking for the mill...' if port == 'auto' else f'Opening {port}...'))
                t = cj.SerialTransport(port, 115200)
                self.sim = None
            g = GuiGrbl(t, log=lambda m: self.post(('log', str(m))))
            g.on_line = lambda line: self.post(('rx', line))
            banner = g.reset()
            g.query()
            self.t, self.g = t, g
            name = 'Simulator' if self.sim else getattr(t, 'port', port)
            self.post(('connected', name, banner))
        except Exception as e:
            msg = str(e)
            if 'Access is denied' in msg or 'PermissionError' in msg:
                msg = f'{port} is busy: close Candle (or any other program using the mill) and connect again'
            self.post(('error', 'Connect failed: ' + hint(msg)))
            self.post(('disconnected',))

    def disconnect(self, quiet=False):
        if self.t is not None:
            try:
                self.t.close()
            except Exception:
                pass
        self.t = self.g = self.sim = None
        if not quiet:
            self.post(('disconnected',))

    # ------------------------------------------------------------------ operator prompts
    def prompt(self, msg, label, dual=True):
        while not self.reply.empty():
            self.reply.get_nowait()
        self.waiting = True
        self.post(('prompt', msg, label, dual))
        if self.sim is not None:
            if 'CARVING' in msg:
                self.sim.mount(1)
            elif 'DRILL' in msg:
                self.sim.mount(2)
        try:
            while True:
                if self.abort:
                    raise Abort()
                try:
                    r = self.reply.get_nowait()
                except queue.Empty:
                    r = None
                if r == 'go':
                    return True
                if r == 'cancel':
                    raise Abort()
                self.service()
        finally:
            self.waiting = False
            self.post(('prompt_clear',))

    def check(self):
        if self.abort:
            raise Abort()

    def circuit_check(self):
        g = self.g
        st = g.query()
        if st.get('probe'):
            raise cj.GrblError('the PROBE light is already ON: the bit is touching the copper or the clips touch each '
                               'other. Lift the bit / separate the clips, then start again.')
        self.post(('prompt', 'CIRCUIT CHECK: touch the free probe lead to the copper (or to the copper clip) for a '
                             'second. The PROBE light must turn green, then let go.', None, False))
        if self.sim is not None:
            self.sim.touch_pulse = 6
        seen, t0 = False, time.time()
        try:
            while time.time() - t0 < 180:
                self.check()
                try:
                    if self.reply.get_nowait() == 'cancel':
                        raise Abort()
                except queue.Empty:
                    pass
                g.pump(0.05)
                p = bool(g.status.get('probe', False))
                seen |= p
                if seen and not p:
                    self.post(('log', 'Circuit check OK.'))
                    return
            raise cj.GrblError('circuit check timed out: the probe never closed. Check both clips and the probe plug.')
        finally:
            self.post(('prompt_clear',))

    # ------------------------------------------------------------------ job
    def next_step(self):
        self.step_i += 1
        self.post(('step', self.step_i))

    def run_job(self, spec):
        g = self.g
        self.job_running, self.abort, self.step_i = True, False, -1
        lv = cj.Leveler(g, self)
        os.makedirs(RUNS, exist_ok=True)
        self.post(('job_start', spec))
        try:
            st = g.query()
            if st['state'].startswith('Alarm'):
                raise cj.GrblError('machine is in ALARM: click Home (or Unlock) before starting')
            g.cmd('G21 G90 G94 G17')
            g.cmd('G92.1')
            g.cmd('M5')
            files = spec['files']
            if spec.get('maptest'):
                return self.map_test(lv, spec)
            paths = [p for p in files.values() if p]
            bx0, by0, bx1, by1 = cj.cut_bounds(paths)
            m = spec['margin']
            bounds = (bx0 - m, by0 - m, bx1 + m, by1 + m)
            ref = (bounds[0], bounds[1])
            self.post(('log', f'Probe area X {bounds[0]:.1f}..{bounds[2]:.1f}  Y {bounds[1]:.1f}..{bounds[3]:.1f}'))
            groups = []
            if files.get('traces'):
                groups.append(('CARVING bit (V-bit / Dremel 105)', ['traces']))
            dr = [r for r in ('holes', 'cutout') if files.get(r)]
            if dr:
                groups.append(('DRILL bit (1.0 mm)', dr))
            for gi, (bit, roles) in enumerate(groups):
                self.next_step()
                verb = 'Put' if gi == 0 else 'SWAP TO'
                self.prompt(f'{verb} the {bit} in the collet (stick-out at least 10 mm, collet tight). Put one probe '
                            f'clip on the copper; keep the other probe lead FREE for the circuit check. Jog the bit '
                            f'5-15 mm above the copper near X0 Y0 (arrow keys / Z / X work now).',
                            f'{bit.split(" (")[0]} loaded, copper clip on')
                self.next_step()
                self.circuit_check()
                self.next_step()
                self.clip_on_prompt()
                self.next_step()
                hm = self.level(lv, bounds, ref, spec, tag=roles[0])
                self.next_step()
                self.prompt('REMOVE the probe clips from the bit (and keep them clear of the spindle). The spindle '
                            'starts as soon as you confirm.', 'Probe clips removed - start the spindle and cut')
                for r in roles:
                    self.next_step()
                    self.stream_file(files[r], r, hm, spec)
                g.cmd('M5')
                g.cmd('G0 Z10')
            self.next_step()
            self.post(('done', 'JOB DONE. Spindle off. Snip the tabs, deburr, check every net with the meter.'))
        except Abort:
            self.post(('log', 'Job stopped.'))
            self.safe_stop()
        except cj.GrblError as e:
            self.post(('error', 'JOB STOPPED: ' + hint(str(e))))
            self.safe_stop()
        except Exception as e:
            self.post(('error', f'JOB STOPPED: {type(e).__name__}: {e}'))
            self.post(('log', traceback.format_exc()))
            self.safe_stop()
        finally:
            g.motion_filter = None
            self.job_running = False
            self.post(('job_end',))

    def safe_stop(self):
        try:
            self.g.motion_filter = None
            self.g.pending.clear()
            self.g.realtime(b'!')
            time.sleep(0.2)
            self.g.realtime(b'\x18')
            time.sleep(0.5)
            self.g.pump(0.2)
        except Exception:
            pass

    def level(self, lv, bounds, ref, spec, tag):
        self.post(('log', 'Finding Z0 (blind touch)...'))
        lv.set_z0_here(lv.touch_blind(*ref))
        self.post(('log', f'Z0 set on the copper at X{ref[0]:.2f} Y{ref[1]:.2f}'))
        nx = max(2, math.ceil((bounds[2] - bounds[0]) / spec['grid']) + 1)
        ny = max(2, math.ceil((bounds[3] - bounds[1]) / spec['grid']) + 1)
        total = nx * ny
        self.post(('log', f'Mapping {nx} x {ny} = {total} points (each point: fast touch + 1 confirming touch, more only if they disagree)...'))
        orig = cj.Leveler.touch
        count = [0]

        def touch(x, y, z_est, travel):
            self.check()
            z = orig(lv, x, y, z_est, travel)
            count[0] += 1
            self.post(('probe_pt', x, y, z, count[0], total))
            return z

        lv.touch = touch
        try:
            self.post(('map_clear',))
            hm = lv.grid(bounds, spec['grid'])
        finally:
            del lv.touch
        path = os.path.join(RUNS, time.strftime('%Y%m%d_%H%M%S') + f'_{tag}.heightmap.json')
        cj._save(hm, list(ref), path)
        self.post(('map', hm.xs, hm.ys, [list(map(float, r)) for r in hm.z], hm.range))
        if self.g.noise_events:
            self.post(('log', f'Probe noise filtered: {self.g.noise_events} events so far (readings re-checked). Many '
                              f'events = route/shorten the probe wires or add a 100 nF capacitor on the probe input.'))
        verdict = 'GOOD' if hm.range < 0.2 else 'OK (compensated)'
        self.post(('log', f'Height map: range {hm.range * 1000:.0f} um ({verdict}). Saved {path}'))
        return hm

    def clip_on_prompt(self):
        self.prompt('Circuit OK. Now clip the free probe lead onto the BIT. Check the bit is still 5-15 mm above the '
                    'copper. The machine starts moving down to touch the copper as soon as you confirm.',
                    'Probe lead is on the bit - start probing')

    def map_test(self, lv, spec):
        w, h = spec['copper_w'], spec['copper_h']
        bounds = (5.0, 5.0, w - 5.0, h - 5.0)
        self.next_step()
        self.prompt('HEIGHT MAP TEST (nothing is cut). Put one probe clip on the copper; keep the other probe lead FREE '
                    'for the circuit check. Bit 5-15 mm above the copper near X0 Y0.', 'Copper clip on, bit above the '
                    'copper')
        self.next_step()
        self.circuit_check()
        self.next_step()
        self.clip_on_prompt()
        self.next_step()
        self.level(lv, bounds, (bounds[0], bounds[1]), spec, tag='test')
        self.g.cmd('G0 Z10')
        self.next_step()
        self.post(('done', 'Height map test done. Take the probe clips off.'))

    def stream_file(self, path, role, hm, spec):
        g = self.g
        name = os.path.basename(path)
        lines = cj.leveled_lines(path, hm)
        if not any(re.search(r'\bM0?3\b', l.upper()) for l in lines):
            lines = [f'M3 S{spec["spindle"]:g}', 'G4 P3'] + lines
        bl = cj.Backlash(spec['backlash_x'], spec['backlash_y'])
        g.motion_filter = bl if bl.active else None
        n = len(lines)
        self.post(('stream', role, name, 0, n))
        self.post(('log', f'Cutting {name}: {n} lines' + (f', backlash comp X{bl.b["X"]} Y{bl.b["Y"]}'
                                                          if bl.active else '')))
        for k, line in enumerate(lines):
            self.check()
            g.send(line)
            if k % 20 == 0:
                self.post(('stream', role, name, k, n))
        g.motion_filter = None
        while True:
            self.check()
            st = g.wait_idle(timeout=4 * 3600)
            if st:
                break
        self.post(('stream', role, name, n, n))
        self.post(('log', f'Finished {name}.'))


# ======================================================================================== GUI
BG, PANEL, FG, DIM = '#1e1f24', '#2a2c33', '#e8e8e8', '#9aa0a6'
STATE_COL = {'Idle': '#3ddc84', 'Run': '#4fc3f7', 'Jog': '#80deea', 'Hold': '#ffd54f', 'Alarm': '#ff5252',
             'Home': '#ce93d8', 'Door': '#ffb74d', 'Check': '#b0bec5', 'Sleep': '#b0bec5'}
ROLE_COL = {'traces': '#4fc3f7', 'holes': '#ff6e6e', 'cutout': '#ffb74d'}


class App:
    def __init__(self, root):
        self.root = root
        self.s = load_settings()
        self.post_q = queue.Queue()
        self.m = Machine(self.post_q.put)
        self.m.start()
        self.connected = False
        self.status = {}
        self.step_idx = 0
        self.keys_down = set()
        self.preview = {}
        self.map_pts = []
        self.map_info = None
        self.trail = []
        self.cutting_role = None
        self.steps = []
        self.step_labels = []
        self.job_t0 = None
        self.stream_info = None
        self.last_rx = time.time()
        self.paused = False
        self.build()
        q = self.post_q

        class _Out:
            buf = ''

            def write(self, t):
                self.buf += t
                while '\n' in self.buf:
                    line, self.buf = self.buf.split('\n', 1)
                    line = line.replace('\r', '').strip()
                    if line:
                        q.put(('log', line))

            def flush(self):
                pass

        sys.stdout = sys.stderr = _Out()
        for r in ('traces', 'holes', 'cutout'):
            if self.s.get(r) and os.path.exists(self.s[r]):
                self.set_file(r, self.s[r], save=False)
        self.refresh_ports()
        if sys.platform == 'win32' and not self.s.get('shortcut_asked') and not os.environ.get('CURSA_AUTOTEST'):
            self.root.after(800, self.offer_shortcut)
        self.root.after(50, self.pump)
        self.root.after(200, self.tick)

    # ------------------------------------------------------------------ layout
    def build(self):
        r = self.root
        r.title(f'{APP} {VERSION}  -  Genmitsu 3018-PROVer V2')
        r.configure(bg=BG)
        r.geometry('1440x880')
        r.minsize(1200, 720)
        try:
            r.state('zoomed')          # Windows: start maximised
        except tk.TclError:
            pass
        st = ttk.Style()
        try:
            st.theme_use('clam')
        except tk.TclError:
            pass
        st.configure('.', background=BG, foreground=FG, fieldbackground=PANEL, font=('Segoe UI', 10))
        st.configure('TFrame', background=BG)
        st.configure('P.TFrame', background=PANEL)
        st.configure('TLabelframe', background=BG, foreground=FG, bordercolor='#444')
        st.configure('TLabelframe.Label', background=BG, foreground='#8ab4f8', font=('Segoe UI', 10, 'bold'))
        st.configure('TButton', background='#3a3d46', foreground=FG, padding=5, bordercolor='#555')
        st.map('TButton', background=[('active', '#4a4e5a'), ('disabled', '#2a2c33')],
               foreground=[('disabled', '#666')])
        st.configure('Go.TButton', background='#1b8a3a', font=('Segoe UI', 13, 'bold'), padding=10)
        st.map('Go.TButton', background=[('active', '#22a547'), ('disabled', '#2a3a2e')])
        st.configure('Stop.TButton', background='#c62828', font=('Segoe UI', 14, 'bold'), padding=10)
        st.map('Stop.TButton', background=[('active', '#e53935')])
        st.configure('Start.TButton', background='#1565c0', font=('Segoe UI', 12, 'bold'), padding=8)
        st.map('Start.TButton', background=[('active', '#1e88e5'), ('disabled', '#26303d')])
        st.configure('TCheckbutton', background=PANEL, foreground=FG, font=('Segoe UI', 11, 'bold'))
        st.map('TCheckbutton', background=[('active', PANEL)])
        st.configure('Horizontal.TProgressbar', troughcolor=PANEL, background='#4fc3f7')
        st.configure('TCombobox', fieldbackground=PANEL, background='#3a3d46', foreground=FG)
        st.map('TCombobox', fieldbackground=[('readonly', PANEL)], foreground=[('readonly', FG)],
               selectbackground=[('readonly', PANEL)], selectforeground=[('readonly', FG)])
        r.option_add('*TCombobox*Listbox.background', PANEL)
        r.option_add('*TCombobox*Listbox.foreground', FG)
        r.option_add('*TCombobox*Listbox.selectBackground', '#1565c0')

        # top bar
        top = ttk.Frame(r)
        top.pack(side='top', fill='x', padx=8, pady=6)
        ttk.Label(top, text='CURSA', font=('Segoe UI', 16, 'bold'), foreground='#ff7043').pack(side='left')
        ttk.Label(top, text=' Mill Studio', font=('Segoe UI', 16)).pack(side='left', padx=(0, 20))
        ttk.Label(top, text='Port').pack(side='left')
        self.port_var = tk.StringVar(value='Auto (find mill)')
        self.port_box = ttk.Combobox(top, textvariable=self.port_var, width=20, state='readonly', takefocus=0,
                                     postcommand=self.refresh_ports)
        self.port_box.pack(side='left', padx=4)
        self.port_box.bind('<<ComboboxSelected>>', lambda e: self.canvas.focus_set())
        self.conn_btn = ttk.Button(top, text='Connect', command=self.toggle_connect, takefocus=0)
        self.conn_btn.pack(side='left', padx=2)
        self.state_lbl = tk.Label(top, text='DISCONNECTED', bg='#444', fg='black', font=('Segoe UI', 13, 'bold'),
                                  width=13)
        self.state_lbl.pack(side='left', padx=10)
        self.probe_led = tk.Label(top, text=' PROBE ', bg='#333', fg='#888', font=('Segoe UI', 11, 'bold'))
        self.probe_led.pack(side='left')
        self.spin_lbl = tk.Label(top, text=' SPINDLE OFF ', bg='#333', fg='#888', font=('Segoe UI', 11, 'bold'))
        self.spin_lbl.pack(side='left', padx=6)
        ttk.Button(top, text='Settings', command=self.settings_dialog, takefocus=0).pack(side='right', padx=2)
        ttk.Button(top, text='STOP (Esc)', style='Stop.TButton', command=self.stop, takefocus=0, width=10).pack(
            side='right', padx=(4, 12))
        self.pause_btn = ttk.Button(top, text='Pause', command=self.pause, takefocus=0, width=9)
        self.pause_btn.pack(side='right', padx=4, ipady=6)
        ttk.Button(top, text='Help', command=self.help, takefocus=0).pack(side='right', padx=2)
        ttk.Button(top, text='Log', command=self.open_folder, takefocus=0).pack(side='right', padx=2)

        body = ttk.Frame(r)
        body.pack(side='top', fill='both', expand=True, padx=8)
        left = ttk.Frame(body, width=330)
        left.pack(side='left', fill='y')
        left.pack_propagate(False)
        right = ttk.Frame(body, width=380)
        right.pack(side='right', fill='y')
        right.pack_propagate(False)
        center = ttk.Frame(body)
        center.pack(side='left', fill='both', expand=True, padx=8)

        # ---- left: DRO + machine + jog
        dro = ttk.LabelFrame(left, text='Position (work)')
        dro.pack(fill='x', pady=(0, 6))
        self.dro = {}
        for i, ax in enumerate('XYZ'):
            f = ttk.Frame(dro)
            f.pack(fill='x', padx=6, pady=1)
            ttk.Label(f, text=ax, font=('Consolas', 22, 'bold'), foreground='#8ab4f8', width=2).pack(side='left')
            v = tk.StringVar(value='---.---')
            ttk.Label(f, textvariable=v, font=('Consolas', 26, 'bold'), anchor='e', width=10).pack(side='left')
            self.dro[ax] = v
        self.mach_var = tk.StringVar(value='')
        ttk.Label(dro, textvariable=self.mach_var, font=('Consolas', 9), foreground=DIM).pack(anchor='w', padx=8,
                                                                                              pady=(0, 4))
        mach = ttk.LabelFrame(left, text='Machine')
        mach.pack(fill='x', pady=6)
        grid = ttk.Frame(mach)
        grid.pack(fill='x', padx=6, pady=6)
        btns = [('Home', lambda: self.send('home')), ('Unlock', lambda: self.send('unlock')),
                ('Reset', lambda: self.send('reset')), ('Spindle OFF', lambda: self.send('spindle_off')),
                ('Set X/Y zero here', self.zero_xy), ('Go to X0 Y0', self.goto0)]
        for i, (t, c) in enumerate(btns):
            ttk.Button(grid, text=t, command=c, takefocus=0).grid(row=i // 2, column=i % 2, sticky='ew', padx=2,
                                                                   pady=2)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        jog = ttk.LabelFrame(left, text='Jog  (keyboard)')
        jog.pack(fill='x', pady=6)
        sf = ttk.Frame(jog)
        sf.pack(fill='x', padx=6, pady=(6, 2))
        ttk.Label(sf, text='STEP').pack(side='left')
        self.step_var = tk.DoubleVar(value=1.0)
        self.step_lbl = tk.Label(sf, text='1 mm', bg='#ff7043', fg='black', font=('Segoe UI', 14, 'bold'), width=7)
        self.step_lbl.pack(side='left', padx=6)
        for v in STEPS:
            ttk.Button(sf, text=f'{v:g}', width=5, command=lambda v=v: self.set_step(v), takefocus=0).pack(
                side='left', padx=1)
        pad = ttk.Frame(jog)
        pad.pack(pady=6)
        jb = lambda t, a, sgn: ttk.Button(pad, text=t, width=5, takefocus=0, command=lambda: self.jog(a, sgn))
        jb('Y+', 'Y', 1).grid(row=0, column=1, pady=2)
        jb('X-', 'X', -1).grid(row=1, column=0, padx=2)
        jb('X+', 'X', 1).grid(row=1, column=2, padx=2)
        jb('Y-', 'Y', -1).grid(row=2, column=1, pady=2)
        jb('Z+', 'Z', 1).grid(row=0, column=4, padx=(18, 0))
        jb('Z-', 'Z', -1).grid(row=2, column=4, padx=(18, 0))
        ttk.Label(jog, text='Arrows: X / Y     Z key: Z up     X key: Z down\nSPACE: step 10 / 1 / 0.1     ESC: STOP',
                  foreground=DIM, font=('Segoe UI', 9), justify='center').pack(pady=(0, 6))
        self.jog_msg = ttk.Label(left, text='', foreground='#ffd54f', wraplength=290)
        self.jog_msg.pack(fill='x')

        # ---- center: preview
        self.canvas = tk.Canvas(center, bg='#121316', highlightthickness=0, takefocus=1)
        self.canvas.pack(fill='both', expand=True)
        self.canvas.bind('<Configure>', lambda e: self.redraw())
        self.canvas.bind('<Button-1>', lambda e: self.canvas.focus_set())
        leg = ttk.Frame(center)
        leg.pack(fill='x', pady=2)
        for t, c in (('traces', ROLE_COL['traces']), ('holes', ROLE_COL['holes']), ('cut-out', ROLE_COL['cutout']),
                     ('cut so far', '#69f0ae'), ('tool', '#ffffff')):
            tk.Label(leg, text='■ ' + t, fg=c, bg=BG, font=('Segoe UI', 9)).pack(side='left', padx=6)
        self.map_lbl = ttk.Label(leg, text='', foreground='#ffd54f')
        self.map_lbl.pack(side='right')

        # ---- right: files, job, prompt, progress
        ff = ttk.LabelFrame(right, text='Files to cut')
        ff.pack(fill='x')
        self.file_lbl = {}
        for role, title in (('traces', 'Traces'), ('holes', 'Holes'), ('cutout', 'Cut-out')):
            row = ttk.Frame(ff)
            row.pack(fill='x', padx=6, pady=2)
            tk.Label(row, text=title, fg=ROLE_COL[role], bg=BG, font=('Segoe UI', 10, 'bold'), width=7,
                     anchor='w').pack(side='left')
            v = tk.StringVar(value='(none)')
            ttk.Label(row, textvariable=v, foreground=FG, width=22).pack(side='left')
            ttk.Button(row, text='x', width=2, takefocus=0, command=lambda r=role: self.set_file(r, '')).pack(
                side='right')
            ttk.Button(row, text='Open', width=5, takefocus=0, command=lambda r=role: self.pick(r)).pack(side='right')
            self.file_lbl[role] = v
        ttk.Label(ff, text='traces = carving bit,  holes + cut-out = drill bit', foreground=DIM,
                  font=('Segoe UI', 8)).pack(anchor='w', padx=8, pady=(0, 3))
        jb2 = ttk.Frame(right)
        jb2.pack(fill='x', pady=6)
        self.start_btn = ttk.Button(jb2, text='START JOB', style='Start.TButton', command=self.start_job,
                                    takefocus=0)
        self.start_btn.pack(side='left', fill='x', expand=True, padx=(0, 4))
        self.test_btn = ttk.Button(jb2, text='Height map\ntest', command=self.start_maptest, takefocus=0)
        self.test_btn.pack(side='left')

        self.prompt_fr = tk.Frame(right, bg=PANEL, highlightthickness=2, highlightbackground='#444')
        self.prompt_fr.pack(fill='x', pady=4)
        self.prompt_txt = tk.Label(self.prompt_fr, text='Connect, jog the bit over the copper lower-left corner, '
                                                        'click "Set X/Y zero here", pick the files, START JOB.',
                                   bg=PANEL, fg=FG, wraplength=350, justify='left', font=('Segoe UI', 10))
        self.prompt_txt.pack(fill='x', padx=8, pady=8)
        self.confirm_var = tk.BooleanVar(value=False)
        self.confirm_chk = tk.Checkbutton(self.prompt_fr, text='', variable=self.confirm_var, takefocus=0,
                                          command=self.update_go, bg=PANEL, fg=FG, activebackground=PANEL,
                                          activeforeground=FG, selectcolor='#000000', wraplength=330, justify='left',
                                          anchor='w', font=('Segoe UI', 11, 'bold'))
        self.go_btn = ttk.Button(self.prompt_fr, text='GO', style='Go.TButton', command=self.go, takefocus=0)

        pf = ttk.Frame(right)
        pf.pack(fill='x', pady=2)
        self.prog_lbl = ttk.Label(pf, text='', font=('Segoe UI', 10))
        self.prog_lbl.pack(anchor='w')
        self.prog = ttk.Progressbar(pf, maximum=1000)
        self.prog.pack(fill='x', pady=2)
        self.time_lbl = ttk.Label(pf, text='', foreground=DIM)
        self.time_lbl.pack(anchor='w')
        stf = ttk.LabelFrame(right, text='Job steps')
        stf.pack(fill='both', expand=True, pady=4)
        self.steps_fr = ttk.Frame(stf)
        self.steps_fr.pack(fill='both', expand=True, padx=6, pady=4)


        # ---- log
        lf = ttk.Frame(r)
        lf.pack(side='bottom', fill='x', padx=8, pady=(0, 6))
        self.log_txt = tk.Text(lf, height=4, bg='#121316', fg='#c8c8c8', font=('Consolas', 9), relief='flat',
                               takefocus=0)
        self.log_txt.pack(fill='x')
        self.log_txt.tag_configure('err', foreground='#ff6e6e')
        self.log_txt.tag_configure('rx', foreground='#8ab4f8')
        self.log_txt.configure(state='disabled')

        for seq in ('<KeyPress>',):
            r.bind(seq, self.on_key)
        r.bind('<KeyRelease>', self.on_key_up)
        self.set_steps([])
        self.canvas.focus_set()
        self.log(f'{APP} {VERSION}. Settings: {SETTINGS}')

    # ------------------------------------------------------------------ helpers
    def log(self, text, tag=None):
        try:
            with open(os.path.join(HOME, 'log.txt'), 'a', encoding='utf-8') as fh:
                stamp = time.strftime('%Y-%m-%d %H:%M:%S')
                for line in str(text).rstrip('\n').split('\n'):
                    fh.write(f'{stamp}  {line}\n')
        except Exception:
            pass
        t = self.log_txt
        t.configure(state='normal')
        for line in str(text).rstrip('\n').split('\n'):
            t.insert('end', line + '\n', tag)
        if int(t.index('end-1c').split('.')[0]) > 2000:
            t.delete('1.0', '500.0')
        t.see('end')
        t.configure(state='disabled')

    def send(self, *item):
        if not self.connected:
            self.flash('Not connected.')
            return
        self.m.q.put(item)

    def flash(self, msg):
        self.jog_msg.configure(text=msg)
        self.root.after(3500, lambda: self.jog_msg.configure(text='') if self.jog_msg.cget('text') == msg else None)

    def refresh_ports(self):
        vals = ['Auto (find mill)', 'Simulator']
        try:
            from serial.tools import list_ports
            vals += [f'{p.device}  {p.description}' for p in list_ports.comports()]
        except Exception:
            pass
        self.port_box.configure(values=vals)

    def toggle_connect(self):
        if self.connected:
            if self.m.job_running:
                return self.flash('Stop the job first.')
            self.m.q.put(('disconnect',))
            return
        v = self.port_var.get()
        port = 'auto' if v.startswith('Auto') else ('Simulator' if v == 'Simulator' else v.split()[0])
        self.conn_btn.configure(state='disabled')
        self.m.q.put(('connect', port))

    def set_step(self, v):
        self.step_var.set(v)
        self.step_lbl.configure(text=f'{v:g} mm')

    def jog_allowed(self):
        if not self.connected:
            return 'Not connected.'
        if self.m.job_running and not self.m.waiting:
            return 'Jogging is off while the machine is working.'
        state = self.status.get('state', '')
        if state.startswith('Alarm'):
            return 'Machine is in ALARM: Home or Unlock first.'
        return None

    def jog(self, axis, sign):
        why = self.jog_allowed()
        if why:
            return self.flash(why)
        d = self.step_var.get() * sign
        f = self.s['jog_z_feed'] if axis == 'Z' else self.s['jog_xy_feed']
        step = abs(d)
        f = f if step >= 10 else (f * 0.6 if step >= 1 else f * 0.2)
        self.m.q.put(('jog', axis, d, max(20.0, f)))

    def on_key(self, e):
        try:
            if isinstance(e.widget, (tk.Entry, ttk.Entry)) or str(e.widget.winfo_toplevel()) != '.':
                return None
        except Exception:
            return None
        ks = e.keysym
        if ks == 'Escape':
            self.stop()
            return 'break'
        if ks in self.keys_down:
            return 'break'
        keymap = {'Left': ('X', -1), 'Right': ('X', 1), 'Up': ('Y', 1), 'Down': ('Y', -1), 'z': ('Z', 1),
                  'Z': ('Z', 1), 'x': ('Z', -1), 'X': ('Z', -1), 'Prior': ('Z', 1), 'Next': ('Z', -1)}
        if ks == 'space':
            self.keys_down.add(ks)
            cur = self.step_var.get()
            i = min(range(len(STEPS)), key=lambda k: abs(STEPS[k] - cur))
            self.set_step(STEPS[(i + 1) % len(STEPS)])
            return 'break'
        if ks in keymap:
            self.keys_down.add(ks)
            self.jog(*keymap[ks])
            return 'break'
        return None

    def on_key_up(self, e):
        self.keys_down.discard(e.keysym)

    def zero_xy(self):
        if self.m.job_running and not self.m.waiting:
            return self.flash('Not while the machine is working.')
        self.send('zero_xy')

    def goto0(self):
        if self.m.job_running:
            return self.flash('Not during a job.')
        if messagebox.askokcancel(APP, 'Move to X0 Y0 at the CURRENT height?\n\nMake sure the bit is above the '
                                       'fixture walls and cams.'):
            self.send('goto0')
        self.canvas.focus_set()

    # ------------------------------------------------------------------ files
    def pick(self, role):
        p = filedialog.askopenfilename(title=f'Pick the {role.upper()} file', initialdir=self.s.get('last_dir') or HOME,
                                       filetypes=[('G-code', '*.nc *.ngc *.gcode *.tap *.txt'), ('All files', '*.*')])
        self.canvas.focus_set()
        if p:
            self.s['last_dir'] = os.path.dirname(p)
            self.set_file(role, p)

    def set_file(self, role, path, save=True):
        if self.m.job_running:
            return self.flash('Not during a job.')
        if path:
            try:
                self.preview[role] = parse_preview(path)
                cj.cut_bounds([path])
            except Exception as e:
                messagebox.showerror(APP, f'Cannot use {os.path.basename(path)}:\n{e}')
                return
            self.file_lbl[role].set(os.path.basename(path))
        else:
            self.preview.pop(role, None)
            self.file_lbl[role].set('(none)')
        self.s[role] = path
        if save:
            save_settings(self.s)
        self.trail = []
        self.redraw()

    def files(self):
        return {r: self.s.get(r) for r in ('traces', 'holes', 'cutout') if self.s.get(r)}

    # ------------------------------------------------------------------ job control
    def spec(self, **kw):
        sp = {k: self.s[k] for k in ('copper_w', 'copper_h', 'backlash_x', 'backlash_y', 'spindle', 'grid', 'margin')}
        sp['files'] = self.files()
        sp.update(kw)
        return sp

    def start_job(self):
        if not self.connected:
            return self.flash('Connect first.')
        if self.m.job_running:
            return self.flash('A job is already running.')
        f = self.files()
        if not f:
            return messagebox.showinfo(APP, 'Pick at least one file (traces and/or holes).')
        for p in f.values():
            if not os.path.exists(p):
                return messagebox.showerror(APP, f'File not found:\n{p}')
        lst = '\n'.join(f'  {r}: {os.path.basename(p)}' for r, p in f.items())
        if not messagebox.askokcancel(APP, f'Start the job?\n\n{lst}\n\nX/Y zero must already be set on the copper '
                                           f'lower-left corner. Blank locked in the fixture.'):
            return
        self.canvas.focus_set()
        self.trail = []
        self.set_steps(plan_steps(f))
        self.m.q.put(('job', self.spec()))

    def start_maptest(self):
        if not self.connected:
            return self.flash('Connect first.')
        if self.m.job_running:
            return self.flash('A job is already running.')
        self.set_steps([('t', 'load', 'Copper clip on, bit above copper'), ('t', 'circuit', 'Circuit check'),
                        ('t', 'clipon', 'Clip probe lead on the bit'), ('t', 'level', 'Find Z0 + height map'),
                        ('t', 'done', 'Done')])
        self.m.q.put(('job', self.spec(maptest=True)))

    def stop(self):
        if not self.connected:
            return
        self.m.abort = True
        try:
            self.m.g.realtime(b'!')
            self.m.g.realtime(b'\x85')
            self.root.after(250, lambda: self.m.g and self.m.g.realtime(b'\x18'))
        except Exception:
            pass
        self.m.reply.put('cancel')
        self.log('STOP pressed: feed hold + reset. Spindle stops. If the machine shows ALARM: Unlock (or Home).',
                 'err')
        self.paused = False
        self.pause_btn.configure(text='Pause')

    def pause(self):
        if not self.connected:
            return
        try:
            if self.paused:
                self.m.g.realtime(b'~')
                self.pause_btn.configure(text='Pause')
            else:
                self.m.g.realtime(b'!')
                self.pause_btn.configure(text='RESUME')
            self.paused = not self.paused
        except Exception:
            pass

    def go(self):
        if self.go_btn.instate(['disabled']):
            return
        self.m.reply.put('go')
        self.canvas.focus_set()

    def update_go(self):
        self.go_btn.state(['!disabled'] if self.confirm_var.get() else ['disabled'])

    def show_prompt(self, msg, label, dual):
        self.prompt_txt.configure(text=msg, fg='#ffd54f')
        self.prompt_fr.configure(highlightbackground='#ffd54f')
        self.confirm_chk.pack_forget()
        self.go_btn.pack_forget()
        if label is None:          # circuit check: no button, the probe light advances it
            pass
        elif dual:
            self.confirm_var.set(False)
            self.confirm_chk.configure(text=label)
            self.confirm_chk.pack(anchor='w', padx=8, pady=(0, 4))
            self.go_btn.configure(text='GO')
            self.go_btn.pack(fill='x', padx=8, pady=(0, 8))
            self.update_go()
        else:
            self.go_btn.configure(text=label)
            self.go_btn.state(['!disabled'])
            self.go_btn.pack(fill='x', padx=8, pady=(0, 8))
        try:
            self.root.bell()
            self.root.deiconify()
            self.root.lift()
        except Exception:
            pass

    def clear_prompt(self, text='', color=FG):
        self.confirm_chk.pack_forget()
        self.go_btn.pack_forget()
        self.prompt_txt.configure(text=text, fg=color)
        self.prompt_fr.configure(highlightbackground='#444')

    def set_steps(self, steps):
        for w in self.steps_fr.winfo_children():
            w.destroy()
        self.steps = steps
        self.step_labels = []
        for _, _, text in steps:
            l = tk.Label(self.steps_fr, text='○  ' + text, bg=BG, fg=DIM, font=('Segoe UI', 8), anchor='w', pady=0)
            l.pack(fill='x')
            self.step_labels.append(l)
        self.step_idx = -1

    def mark_step(self, i):
        self.step_idx = i
        for k, l in enumerate(self.step_labels):
            text = self.steps[k][2]
            if k < i:
                l.configure(text='✓  ' + text, fg='#3ddc84', font=('Segoe UI', 8))
            elif k == i:
                l.configure(text='►  ' + text, fg='#ffd54f', font=('Segoe UI', 9, 'bold'))
            else:
                l.configure(text='○  ' + text, fg=DIM, font=('Segoe UI', 8))
        if 0 <= i < len(self.steps):
            self.prog_lbl.configure(text=f'Step {i + 1}/{len(self.steps)}: {self.steps[i][2]}')

    def open_folder(self):
        try:
            if sys.platform == 'win32':
                os.startfile(HOME)
            else:
                messagebox.showinfo(APP, HOME)
        except Exception as e:
            messagebox.showerror(APP, str(e))

    def offer_shortcut(self, ask=True):
        self.s['shortcut_asked'] = True
        save_settings(self.s)
        if ask and not messagebox.askyesno(APP, 'Create a Desktop and Start Menu shortcut for CURSA Mill Studio?\n\n'
                                                 'After that you just double-click the shortcut.'):
            return
        try:
            paths = make_shortcuts()
            self.log('Shortcuts created: ' + ', '.join(paths))
            messagebox.showinfo(APP, 'Shortcut created on the Desktop and in the Start Menu.')
        except Exception as e:
            messagebox.showerror(APP, f'Could not create the shortcut:\n{e}')
        self.canvas.focus_set()

    # ------------------------------------------------------------------ settings / help
    def settings_dialog(self):
        d = tk.Toplevel(self.root)
        d.title('Settings')
        d.configure(bg=BG)
        d.transient(self.root)
        fields = [('copper_w', 'Copper blank width X (mm)'), ('copper_h', 'Copper blank height Y (mm)'),
                  ('backlash_x', 'Backlash X from the coupon (mm)'), ('backlash_y', 'Backlash Y from the coupon (mm)'),
                  ('spindle', 'Spindle S (used if a file has no M3)'), ('grid', 'Max probe spacing (mm)'),
                  ('margin', 'Probe margin around the cuts (mm)'), ('jog_xy_feed', 'Jog feed X/Y at step 10 (mm/min)'),
                  ('jog_z_feed', 'Jog feed Z at step 10 (mm/min)')]
        vars_ = {}
        for i, (k, t) in enumerate(fields):
            ttk.Label(d, text=t).grid(row=i, column=0, sticky='w', padx=10, pady=4)
            v = tk.StringVar(value=f'{self.s[k]:g}')
            ttk.Entry(d, textvariable=v, width=10).grid(row=i, column=1, padx=10)
            vars_[k] = v

        def ok():
            for k, v in vars_.items():
                try:
                    self.s[k] = float(v.get())
                except ValueError:
                    messagebox.showerror(APP, f'Not a number: {v.get()}', parent=d)
                    return
            save_settings(self.s)
            d.destroy()
            self.redraw()
            self.canvas.focus_set()

        ttk.Button(d, text='Save', command=ok).grid(row=len(fields), column=0, columnspan=2, pady=10)
        if sys.platform == 'win32':
            ttk.Button(d, text='Create Desktop shortcut', command=lambda: self.offer_shortcut(False)).grid(
                row=len(fields) + 1, column=0, columnspan=2, pady=(0, 10))
        d.grab_set()

    def help(self):
        messagebox.showinfo(APP, __doc__ + f'\nSettings and height maps: {HOME}\nSimulator: pick "Simulator" as the '
                                           'port to try everything without the machine.')
        self.canvas.focus_set()

    # ------------------------------------------------------------------ drawing
    def world_box(self):
        x0, y0, x1, y1 = 0.0, 0.0, self.s['copper_w'], self.s['copper_h']
        for cuts, pl in self.preview.values():
            for poly in cuts:
                for x, y in poly:
                    x0, y0, x1, y1 = min(x0, x), min(y0, y), max(x1, x), max(y1, y)
            for x, y in pl:
                x0, y0, x1, y1 = min(x0, x), min(y0, y), max(x1, x), max(y1, y)
        return x0 - 6, y0 - 6, x1 + 6, y1 + 6

    def tf(self):
        c = self.canvas
        w, h = max(50, c.winfo_width()), max(50, c.winfo_height())
        x0, y0, x1, y1 = self.world_box()
        sc = min((w - 40) / (x1 - x0), (h - 40) / (y1 - y0))
        ox = (w - (x1 - x0) * sc) / 2 - x0 * sc
        oy = (h + (y1 - y0) * sc) / 2 + y0 * sc
        return lambda x, y: (ox + x * sc, oy - y * sc), sc

    def redraw(self):
        c = self.canvas
        c.delete('all')
        P, sc = self.tf()
        self.P, self.sc = P, sc
        a, b = P(0, 0), P(self.s['copper_w'], self.s['copper_h'])
        c.create_rectangle(a[0], b[1], b[0], a[1], fill='#3b2a1a', outline='#c87533', width=2)
        c.create_text(a[0] + 4, b[1] + 4, anchor='nw', fill='#c87533', font=('Segoe UI', 9),
                      text=f'copper {self.s["copper_w"]:g} x {self.s["copper_h"]:g}')
        for x in range(0, int(self.s['copper_w']) + 1, 10):
            p = P(x, 0)
            c.create_line(p[0], a[1], p[0], a[1] + 4, fill='#666')
        if self.map_pts:
            zs = [p[2] for p in self.map_pts]
            lo, hi = min(zs), max(zs)
            for x, y, z in self.map_pts:
                self.draw_map_pt(x, y, z, lo, hi)
        for role in ('cutout', 'holes', 'traces'):
            if role not in self.preview:
                continue
            cuts, pl = self.preview[role]
            col = ROLE_COL[role]
            for poly in cuts:
                pts = [v for xy in poly for v in P(*xy)]
                if len(pts) >= 4:
                    c.create_line(*pts, fill=col, width=1)
            r = max(1.5, 0.5 * sc)
            for x, y in pl:
                q = P(x, y)
                c.create_oval(q[0] - r, q[1] - r, q[0] + r, q[1] + r, outline=col)
        for i in range(1, len(self.trail)):
            p, q = P(*self.trail[i - 1]), P(*self.trail[i])
            c.create_line(*p, *q, fill='#69f0ae', width=2, tags='trail')
        o = P(0, 0)
        c.create_line(o[0], o[1], o[0] + 30, o[1], fill='#ff5252', width=2, arrow='last')
        c.create_line(o[0], o[1], o[0], o[1] - 30, fill='#3ddc84', width=2, arrow='last')
        c.create_text(o[0] - 4, o[1] + 4, anchor='ne', text='X0 Y0', fill=DIM, font=('Segoe UI', 8))
        self.draw_tool()

    def draw_map_pt(self, x, y, z, lo, hi):
        t = 0.5 if hi - lo < 1e-9 else (z - lo) / (hi - lo)
        r_, g_, b_ = int(40 + 215 * t), int(120 + 60 * (1 - abs(t - 0.5) * 2)), int(255 * (1 - t))
        col = f'#{r_:02x}{g_:02x}{b_:02x}'
        p = self.P(x, y)
        s = max(3, 1.2 * self.sc)
        self.canvas.create_rectangle(p[0] - s, p[1] - s, p[0] + s, p[1] + s, fill=col, outline='', tags='map')

    def draw_tool(self):
        c = self.canvas
        c.delete('tool')
        w = self.status.get('wpos')
        if not w or not hasattr(self, 'P'):
            return
        p = self.P(w[0], w[1])
        c.create_oval(p[0] - 7, p[1] - 7, p[0] + 7, p[1] + 7, outline='#ffffff', width=2, tags='tool')
        c.create_line(p[0] - 12, p[1], p[0] + 12, p[1], fill='#ffffff', tags='tool')
        c.create_line(p[0], p[1] - 12, p[0], p[1] + 12, fill='#ffffff', tags='tool')

    # ------------------------------------------------------------------ event pump
    def tick(self):
        try:
            if self.connected and self.m.g is not None:
                self.m.g.realtime(b'?')
        except Exception:
            pass
        self.root.after(200, self.tick)

    def pump(self):
        n = 0
        try:
            while n < 400:
                item = self.post_q.get_nowait()
                n += 1
                self.handle(item)
        except queue.Empty:
            pass
        if self.connected and self.m.g is not None:
            st = self.m.g.status
            if st and 'state' in st:
                self.update_status(st)
        self.root.after(50, self.pump)

    def update_status(self, st):
        self.status = st
        state = st['state'].split(':')[0]
        self.state_lbl.configure(text=st['state'].upper(), bg=STATE_COL.get(state, '#888'))
        w, m = st.get('wpos'), st.get('mpos')
        if w:
            for i, ax in enumerate('XYZ'):
                self.dro[ax].set(f'{w[i]:8.3f}')
            if m:
                self.mach_var.set(f'machine X{m[0]:.2f} Y{m[1]:.2f} Z{m[2]:.2f}')
        p = st.get('probe')
        self.probe_led.configure(bg='#3ddc84' if p else '#333', fg='black' if p else '#888')
        fs = st.get('FS', '0,0').split(',')
        spin = len(fs) > 1 and float(fs[1] or 0) > 0
        if self.m.sim is not None:
            spin = bool(self.m.sim.spindle)
        self.spin_lbl.configure(text=' SPINDLE ON ' if spin else ' SPINDLE OFF ', bg='#ff7043' if spin else '#333',
                                fg='black' if spin else '#888')
        if w and self.cutting_role and spin and hasattr(self, 'P'):
            pt = (w[0], w[1])
            if not self.trail or math.dist(pt, self.trail[-1]) > 0.05:
                if self.trail:
                    a, b = self.P(*self.trail[-1]), self.P(*pt)
                    self.canvas.create_line(*a, *b, fill='#69f0ae', width=2, tags='trail')
                self.trail.append(pt)
        self.draw_tool()

    def handle(self, item):
        k = item[0]
        if k == 'log':
            self.log(item[1])
        elif k == 'rx':
            line = item[1]
            self.log(hint(line), 'err' if line.startswith(('ALARM', 'error', '[MSG:Estop')) else 'rx')
        elif k == 'error':
            self.last_error = item[1]
            self.log(item[1], 'err')
            self.flash(item[1][:160])
        elif k == 'connected':
            self.connected = True
            self.conn_btn.configure(text='Disconnect', state='normal')
            self.log(f'Connected: {item[1]}  ({item[2]})')
            if item[1] == 'Simulator':
                self.log('SIMULATOR: a fake mill with a warped 102 x 71 blank. X/Y zero is already on its corner.')
        elif k == 'disconnected':
            self.connected = False
            self.conn_btn.configure(text='Connect', state='normal')
            self.state_lbl.configure(text='DISCONNECTED', bg='#444')
            self.status = {}
        elif k == 'prompt':
            self.show_prompt(item[1], item[2], item[3])
        elif k == 'prompt_clear':
            self.clear_prompt()
        elif k == 'step':
            self.mark_step(item[1])
            key = self.steps[item[1]][1] if 0 <= item[1] < len(self.steps) else ''
            self.cutting_role = key.split(':')[1] if key.startswith('cut:') else None
            if not key.startswith('cut:'):
                self.prog['value'] = 0
                self.time_lbl.configure(text='')
        elif k == 'job_start':
            self.last_error = ''
            self.job_t0 = time.time()
            self.start_btn.state(['disabled'])
            self.test_btn.state(['disabled'])
        elif k == 'job_end':
            self.cutting_role = None
            self.start_btn.state(['!disabled'])
            self.test_btn.state(['!disabled'])
            self.paused = False
            self.pause_btn.configure(text='Pause')
            if self.step_idx < len(self.steps) - 1:
                why = getattr(self, 'last_error', '') or 'stopped by you (STOP / cancel)'
                self.clear_prompt(f'JOB STOPPED: {why}\n\nFix the cause, then START JOB again (it re-probes from the '
                                  f'start). Full log: {os.path.join(HOME, "log.txt")}', '#ff6e6e')
        elif k == 'done':
            self.mark_step(len(self.steps) - 1)
            self.step_labels[-1].configure(text='✓  Done', fg='#3ddc84')
            el = time.time() - (self.job_t0 or time.time())
            self.clear_prompt(item[1] + f'\n(total {el / 60:.0f} min)', '#3ddc84')
            self.prog['value'] = 1000
            try:
                self.root.bell()
            except Exception:
                pass
        elif k == 'map_clear':
            self.map_pts = []
            self.canvas.delete('map')
            self.map_lbl.configure(text='probing...')
        elif k == 'probe_pt':
            _, x, y, z, cnt, tot = item
            self.map_pts.append((x, y, z))
            zs = [p[2] for p in self.map_pts]
            self.draw_map_pt(x, y, z, min(zs), max(zs))
            self.prog['value'] = min(1000, 1000 * cnt / max(1, tot))
            self.time_lbl.configure(text=f'probe point {cnt} / ~{tot}')
        elif k == 'map':
            _, xs, ys, z, rng = item
            self.map_pts = [(x, y, z[i][j]) for i, x in enumerate(xs) for j, y in enumerate(ys)]
            self.map_lbl.configure(text=f'height map range {rng * 1000:.0f} um  '
                                        f'({"GOOD" if rng < 0.2 else "compensated"})')
            self.redraw()
        elif k == 'stream':
            _, role, name, i, n = item
            if self.stream_info is None or self.stream_info[0] != name:
                self.stream_info = (name, time.time())
            self.prog['value'] = 1000 * i / max(1, n)
            el = time.time() - self.stream_info[1]
            eta = el / i * (n - i) if i > 20 else None
            self.time_lbl.configure(text=f'{name}: line {i} / {n}   {el / 60:.1f} min' +
                                    (f'   ~{eta / 60:.1f} min left (buffered)' if eta is not None else ''))


def main():
    root = tk.Tk()
    app = App(root)
    if os.environ.get('CURSA_AUTOTEST'):
        autotest(app)
    root.mainloop()


def autotest(app):
    """Headless end-to-end test against the simulator: connect, load files, run the job, answer every prompt."""
    out = os.environ['CURSA_AUTOTEST']
    files = json.loads(os.environ.get('CURSA_AUTOTEST_FILES', '{}'))
    app.port_var.set('Simulator')
    app.toggle_connect()

    def later():
        if not app.connected:
            return app.root.after(200, later)
        for r, p in files.items():
            app.set_file(r, p, save=False)
        sim = app.m.sim
        m0 = list(sim.m)
        c = app.canvas
        c.focus_force()
        for ks in ('Right', 'Right', 'space', 'Up', 'space', 'z', 'x', 'x', 'space'):
            c.event_generate(f'<KeyPress-{ks}>', when='tail')
            c.event_generate(f'<KeyRelease-{ks}>', when='tail')
        app.root.update()
        time.sleep(0.5)
        app.root.update()
        app.keytest = [round(a - b, 3) for a, b in zip(sim.m, m0)] + [app.step_var.get()]
        if os.environ.get('CURSA_MAPTEST'):
            app.start_maptest()
        else:
            app.set_steps(plan_steps(app.files()))
            app.m.q.put(('job', app.spec()))
        app.root.after(300, answer)

    shots = [0]

    def answer():
        if app.go_btn.winfo_ismapped():
            if os.environ.get('CURSA_SHOT') and shots[0] < 3:
                shots[0] += 1
                app.root.update()
                os.system(f'import -window root {os.environ["CURSA_SHOT"]}_{shots[0]}.png')
            app.confirm_var.set(True)
            app.update_go()
            app.go()
        if not app.m.job_running and app.step_idx >= len(app.steps) - 1 and app.steps:
            app.root.update()
            if os.environ.get('CURSA_SHOT'):
                os.system(f'import -window root {os.environ["CURSA_SHOT"]}_done.png')
            sim = app.m.sim
            d1 = [c[2] for c in sim.cuts[1]] if not os.environ.get('CURSA_MAPTEST') else []
            d2 = [c[2] for c in sim.cuts[2]]
            json.dump({'keytest': getattr(app, 'keytest', None), 'map_pts': len(app.map_pts), 'steps_done': app.step_idx, 'n_steps': len(app.steps),
                       'trace_depth': [min(d1), max(d1)] if d1 else None,
                       'drill_depth': sorted(set(round(v, 2) for v in d2)) if d2 else None,
                       'rapid_crash': sim.rapid_crash, 'cut_spindle_off': sim.cut_while_stopped,
                       'log_tail': app.log_txt.get('end-25l', 'end')}, open(out, 'w'), indent=1)
            app.root.destroy()
            return
        app.root.after(150, answer)

    app.root.after(300, later)


if __name__ == '__main__':
    main()
