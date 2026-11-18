"""End-to-end tests against the simulated GRBL machine (warped copper blank, backlash, bit change)."""
import os

import pytest

from cursa_studio import core
from cursa_studio.sim import FakeGrbl

ISO = """G21 G90
G0 Z5
M3 S1000
G0 X10 Y10
G1 Z-0.1 F25
G1 X60 F100
G2 X80 Y30 I0 J20
G1 Y50
G0 Z5
M5
M30
"""
DRILL = "G21 G90\nG0 Z10\n" + "".join(
    f"G0 X{x} Y{y}\nG1 Z-0.8 F60\nG0 Z0.3\nG1 Z-1.9 F60\nG0 Z1\n" for x in (10, 30, 50) for y in (8, 40)) + "G0 Z10\nM30\n"


class AutoUI:
    def __init__(self, fake):
        self.f = fake

    def ask(self, msg):
        if 'CIRCUIT' in msg:
            self.f.touch_pulse = 6

    def confirm(self, msg):
        if 'BIT for 2_drill' in msg:                     # operator swaps to the drill: different stick-out
            self.f.tool_offsets[1] = 3.7
            self.f.mount(2)
            self.f.m[2] = -38 + 14 - 3.7
        return True

    def yesno(self, msg):
        return True


@pytest.fixture
def files(tmp_path):
    a = tmp_path / '1_traces.nc'
    b = tmp_path / '2_drill.nc'
    a.write_text(ISO)
    b.write_text(DRILL)
    return str(a), str(b)


def test_full_job_depths(files, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    f = FakeGrbl(backlash=0.2)
    f.mount(1)
    f.m = [-180.0, -120.0, -38 + 15]
    monkeypatch.setattr(core.Leveler, 'circuit_test', lambda self: None)
    rc = core.main(['job', files[1], files[0], '--backlash-x', '0.2', '--backlash-y', '0.2'], transport=f, ui=AutoUI(f))
    assert rc == 0
    d1 = [c[2] for c in f.cuts[1]]
    d2 = [c[2] for c in f.cuts[2]]
    assert 0.08 < min(d1) and max(d1) < 0.12            # 0.10 commanded, warped blank compensated
    assert any(abs(v - 0.8) < 0.02 for v in d2) and any(abs(v - 1.9) < 0.02 for v in d2)
    assert f.rapid_crash == 0 and f.cut_while_stopped == 0


def test_height_map_test(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    f = FakeGrbl()
    f.mount(1)
    f.m = [-180.0, -120.0, -38 + 12]
    monkeypatch.setattr(core.Leveler, 'circuit_test', lambda self: None)
    assert core.main(['map', '--area', '5', '5', '95', '65'], transport=f, ui=AutoUI(f)) == 0
    assert os.path.exists(tmp_path / 'test.heightmap.json')


def test_preview_parser(files):
    from cursa_studio.app import parse_preview, plan_steps
    cuts, plunges = parse_preview(files[0])
    assert cuts and plunges == [(10.0, 10.0)]
    cuts, plunges = parse_preview(files[1])
    assert len(plunges) == 6
    steps = plan_steps({'traces': files[0], 'holes': files[1]})
    assert [k for _, k, _ in steps].count('circuit') == 2 and steps[-1][1] == 'done'


def test_noisy_probe(files, monkeypatch, tmp_path):
    """Noise: 15 % of probe moves trigger early (read 0.03-0.4 mm high), 3 % false 'closed' / ALARM:4."""
    monkeypatch.chdir(tmp_path)
    f = FakeGrbl(backlash=0.2, noise_early=0.15, noise_status=0.03, seed=7)
    f.mount(1)
    f.m = [-180.0, -120.0, -38 + 15]
    monkeypatch.setattr(core.Leveler, 'circuit_test', lambda self: None)
    rc = core.main(['job', files[0], files[1]], transport=f, ui=AutoUI(f))
    assert rc == 0
    assert f.noise_count > 5                          # the noise really happened
    d1 = [c[2] for c in f.cuts[1]]
    assert 0.08 < min(d1) and max(d1) < 0.12          # depths still right
    assert f.rapid_crash == 0 and f.cut_while_stopped == 0


def test_big_bow_asks_instead_of_stopping(monkeypatch, tmp_path):
    """Over the flatness limit the operator decides: accept -> map returned, refuse -> stop."""
    for accept in (True, False):
        f = FakeGrbl()
        f.mount(1)
        f.m = [-180.0, -120.0, -38 + 12]
        g = core.Grbl(f)
        g.reset()
        lv = core.Leveler(g, AutoUI(f))
        g.cmd('G21 G90')
        lv.set_z0_here(lv.touch_blind(5, 5))
        lv.accept_big_range = lambda hm, limit: accept
        if accept:
            hm = lv.grid((5, 5, 60, 40), 10, range_max=0.05)
            assert hm.range > 0.05
        else:
            with pytest.raises(core.GrblError):
                lv.grid((5, 5, 60, 40), 10, range_max=0.05)


def test_comments_nested_and_check(tmp_path):
    assert core.strip_comment('( a (b) c )') == ''
    assert core.strip_comment('G1 X1 (move (fast)) Y2 ; tail') == 'G1 X1  Y2'
    assert core.strip_comment(')') == ''
    p = tmp_path / 'bad.nc'
    p.write_text('( ok (nested) )\nG0 X1\nhello\nG1 Z-0.1 F25\n')
    assert [n for n, _ in core.check_file(str(p))] == [3]
