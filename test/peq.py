#!/usr/bin/env python3
"""Run the actual C parser/storage and PCM DSP checks."""
import cmath
import ctypes as C
import math
import pathlib
import subprocess
import tempfile
import sys; sys.path.insert(0, sys.path[0] + '/../tools')  # tools/ first
from build import FUNCTIONS, ROOT
from peq import LIBC

PROTOTYPES = {**FUNCTIONS, **LIBC}  # the stock calls the host shims stand in for

class Band(C.Structure):
    _fields_ = [('enabled', C.c_int), ('type', C.c_int), ('frequency', C.c_double),
                ('gain', C.c_double), ('q', C.c_double)]

class Preset(C.Structure):
    _fields_ = [('count', C.c_int), ('bypass', C.c_int), ('preamp', C.c_double), ('bands', Band * 30),
                ('balance', C.c_double)]

class State(C.Structure):
    _fields_ = [('mode', C.c_int), ('hold', C.c_int), ('peq', Preset), ('eq3', Preset)]
EQ_OFF, EQ_3, EQ_PEQ = range(3)

class Error(C.Structure):
    _fields_ = [('line', C.c_uint), ('reason', C.c_char_p)]

class Coeff(C.Structure):
    _fields_ = [(k, C.c_double) for k in ('b0', 'b1', 'b2', 'a1', 'a2')]

class Engine(C.Structure):
    _fields_ = [('c', Coeff * 30), ('z', ((C.c_double * 2) * 30) * 8),
                ('gain', C.c_double * 8), ('bypass', C.c_int), ('used', C.c_int), ('only', C.c_int * 30)]

class DSP(C.Structure):
    _fields_ = [('current', Engine), ('next', Engine), ('pending', Engine),
                *[(k, C.c_int) for k in ('rate', 'channels', 'ramp', 'ramp_length', 'waiting')]]

def compile_host(tmp, name, *sources):
    path = tmp/name
    subprocess.run(['cc', '-DPEQ_HOST', f'-DPEQ_ROOT="{tmp}/root"', '-O2', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                    '-shared', '-fPIC', *map(str, sources), '-lm', '-o', str(path)], check=True)
    return C.CDLL(str(path))

def library(tmp):
    lib = compile_host(tmp, 'peq.so', ROOT/'patch/peq.c')
    signatures = {
        'peq_default': [C.POINTER(Preset)], 'peq_valid': [C.POINTER(Preset)],
        'peq_number': [C.c_char_p, C.POINTER(C.c_double)],
        'peq_parse': [C.c_char_p, C.c_uint, C.POINTER(Preset), C.POINTER(Error)],
        'peq_import_file': [C.c_char_p, C.POINTER(Preset), C.POINTER(Error)],
        'peq_save': [C.c_char_p, C.POINTER(Preset), C.c_int],
        'peq_load': [C.c_char_p, C.POINTER(Preset)],
        'peq_compile': [C.POINTER(Preset), C.c_int, C.POINTER(Engine)],
        'peq_reset': [C.POINTER(DSP), C.c_int, C.c_int, C.POINTER(Preset)],
        'peq_update': [C.POINTER(DSP), C.POINTER(Preset)],
        'peq_process': [C.POINTER(DSP), C.POINTER(C.c_float), C.c_uint],
        'eq3_bands': [C.POINTER(Preset), C.POINTER(C.c_double * 3)],
        'eq_load': [C.POINTER(State)], 'eq_save': [C.POINTER(State)],
        'eq_effective': [C.POINTER(State), C.POINTER(Preset)], 'peq_load_active': [C.POINTER(Preset)],
    }
    for name, args in signatures.items(): getattr(lib, name).argtypes = args
    return lib

def parse(lib, text):
    data = text.encode() if isinstance(text, str) else text
    p, error = Preset(), Error()
    assert lib.peq_parse(data, len(data), C.byref(p), C.byref(error)), (error.line, error.reason, data)
    return p

def parser_check(lib, tmp):
    value = C.c_double()
    for text in (b" 0x1p0", b"\t+0x1p0", b" 1", b"1 "):
        assert not lib.peq_number(text, C.byref(value)), text
    for text, want in ((b"1e3", 1000), (b"-1,5", -1.5), (b"+.7", .7)):
        assert lib.peq_number(text, C.byref(value)) and value.value == want, text
    source = '\ufeff # comment\r\nPreamp : -3 dB # inline\r\nPreamp:\t-2 dB\n'
    aliases = ['PK', 'PEQ', 'LS', 'LSC', 'HS', 'HSC']
    for i, alias in enumerate(aliases):
        source += f'Filter {99-i}: {"ON" if i%2 else "OFF"}\t{alias} Fc {100+i*100} Hz Gain -6 dB'
        source += ' Q 1.3\n' if i < 2 else '\n'
    p = parse(lib, source)
    assert p.count == 6 and p.preamp == -5 and p.bypass == 0
    assert [b.type for b in p.bands[:6]] == [0, 0, 1, 1, 2, 2]
    assert [b.enabled for b in p.bands[:6]] == [0, 1, 0, 1, 0, 1]
    assert [b.frequency for b in p.bands[:6]] == [100, 200, 300, 400, 500, 600]
    a = 10 ** (-6 / 40)  # APO: shelf without Q is slope 0.9 at Fc
    assert abs(p.bands[2].q - 1 / math.sqrt((a + 1/a) * (1/0.9 - 1) + 2)) < 1e-15
    # APO: LS/HS with Q give a corner frequency (S=1 at Q 0.7071 -> shift 10^(gain/80)); LSC/HSC the centre.
    shelves = parse(lib, 'Filter: ON LS Fc 100 Hz Gain 12 dB Q 0.7071067811865476\n'
                         'Filter: ON HS Fc 8000 Hz Gain -12 dB Q 0.7071067811865476\n'
                         'Filter: ON LSC Fc 100 Hz Gain 12 dB Q 0.7\n'
                         'Filter 11: OFF None\nFilter: ON PK Fc 1000,5 Hz Gain -3,5 dB Q 1,41\n')
    assert abs(shelves.bands[0].frequency - 100 * 10**(12/80)) < 1e-9
    assert abs(shelves.bands[1].frequency - 8000 / 10**(12/80)) < 1e-9
    assert shelves.bands[2].frequency == 100 and shelves.count == 4
    assert (shelves.bands[3].frequency, shelves.bands[3].gain, shelves.bands[3].q) == (1000.5, -3.5, 1.41)
    edges = parse(lib, 'Filter: ON LS Fc 19000 Hz Gain 12 dB Q 0.7\nFilter: ON HS Fc 21 Hz Gain -12 dB Q 0.7\n')
    assert (edges.bands[0].frequency, edges.bands[1].frequency) == (20000, 20)
    single = 'Filter: ON PK Fc 1e3 Hz Gain +6.0 dB Q .7\n'
    assert parse(lib, single).preamp == 0
    assert parse(lib, 'Preamp: 24 dB\nPreamp: 24 dB\nPreamp: -48 dB').preamp == 0
    assert parse(lib, single * 30).count == 30
    assert parse(lib, single + 'Filter 2: OFF PK Fc 0 Hz Gain 0.0 dB Q 0.000').count == 1  # blank slot
    assert parse(lib, '').count == 0
    # Import is pure. Saving succeeds only after validation and explicit collision confirmation.
    active, saved = tmp/'active', tmp/'preset'
    for path in (active, saved): assert lib.peq_save(bytes(path), C.byref(p), 0) == 1
    baseline = bytes(p)
    failures = [
        (single * 30 + single.replace('ON', 'OFF'), 31, '30 bands'),
        ('Preamp: 24 dB\nPreamp: 1 dB', 2, 'summed'),
        ('Preamp: -61 dB', 1, 'preamp'),
        ('Preamp: -3', 1, 'expected'),
        ('\nGraphicEQ: 20 0; 20000 0', 2, 'unsupported'),
        ('Include: other.txt', 1, 'unsupported'), ('Channel: C', 1, 'only channels'), ('Channel:', 1, 'expected Channel'),
        ('Channel: L\nPreamp: -13 dB', 2, 'differ'),
        ('Eval: x=1', 1, 'unsupported'), ('garbage', 1, 'unsupported'),
        (single.replace(' Q .7', ''), 1, 'requires Q'),
        (single.replace('PK', 'HP'), 1, 'type'),
        (single.replace('PK', 'LS 6dB'), 1, 'form'),
        (single.replace('Q .7', 'BW Oct 1'), 1, 'form'),
        (single.replace('1e3', '`1000`'), 1, 'number'),
        (single.replace('Filter:', 'Filter x:'), 1, 'label'),
        (single + 'Filter: ON PK Fc 19 Hz Gain 0 dB Q 1', 2, 'range'),
        (b'\0', 1, 'NUL'), (b'#' * 513, 1, 'line'), (b'#' * 16385, 1, 'file'),
    ]
    for old, values in [('1e3', ['nan', 'inf', '-inf', '1e999', '20junk', '0x100', '20001', '-20']),
                        ('+6.0', ['nan', '25', '-25']), ('.7', ['NaN', '0', '-1', '10.1'])]:
        failures += [(single.replace(old, value), 1, 'number' if value in ('nan', 'inf', '-inf', '1e999', '20junk', '0x100', 'NaN') else 'range') for value in values]
    for text, line, reason in failures:
        data = text.encode() if isinstance(text, str) else text
        error = Error()
        assert not lib.peq_parse(data, len(data), C.byref(p), C.byref(error)), data
        assert error.line == line and reason in error.reason.decode(), (data, error.line, error.reason)
        assert bytes(p) == baseline
        assert active.read_bytes() == saved.read_bytes()
    q = parse(lib, single)
    before = saved.read_bytes()
    assert lib.peq_save(bytes(saved), C.byref(q), 0) == 2
    assert saved.read_bytes() == before  # cancel
    assert lib.peq_save(bytes(saved), C.byref(q), 1) == 1  # confirm
    assert active.read_bytes() == before  # never auto-activate
    source_file = tmp/'input.txt'
    source_file.write_text(single)
    assert lib.peq_import_file(bytes(source_file), C.byref(p), C.byref(Error()))
    source_file.unlink()
    assert lib.peq_load(bytes(saved), C.byref(p)) and bytes(p) == bytes(q)
    unchanged = bytes(p)
    for path in (source_file, tmp):
        e = Error()
        assert not lib.peq_import_file(bytes(path), C.byref(p), C.byref(e))
        assert e.line and e.reason and bytes(p) == unchanged
    saved.write_bytes(saved.read_bytes()[:-1])
    assert not lib.peq_load(bytes(saved), C.byref(p)) and bytes(p) == unchanged
    # APO Channel scopes: bands follow it, per-channel preamps become preamp + balance.
    scoped = parse(lib, 'Preamp: -3 dB\nChannel: L\nPreamp: -1.5 dB\n' + single + 'Channel: R\nPreamp: -0.5 dB\n'
                        + single + 'Channel: L R\n' + single + 'Channel: all\n' + single.replace('ON', 'OFF'))
    assert [b.enabled for b in scoped.bands[:4]] == [2, 3, 1, 0]
    assert parse(lib, single * 30 + 'Filter: OFF PK Fc 0 Hz Gain 0 dB Q 0').count == 30  # blank slots do not count
    assert scoped.preamp == -3.5 and scoped.balance == 1  # left 1 dB below right
    # v1 files (no balance) still load, with the balance centred.
    v1 = tmp/'v1'
    v1.write_bytes(b'Q2PEQ01\0' + bytes(q)[:16 + 10*32])
    assert lib.peq_load(bytes(v1), C.byref(p)) and bytes(p) == bytes(q)  # q's bands 11-30 are the defaults
    v1.write_bytes(b'Q2PEQ02\0' + bytes(q)[:-8])
    assert not lib.peq_load(bytes(v1), C.byref(p))
    v1.write_bytes(b'')
    assert not lib.peq_load(bytes(v1), C.byref(p))
    assert not lib.peq_save(bytes(tmp/'missing'/'preset'), C.byref(q), 1)
    print('PEQ parser/storage: syntax, limits, transaction failures, confirmation and persistence passed.')

def response(engine, rate, frequency, ch=0):
    if engine.bypass: return 1
    z = cmath.exp(-2j * math.pi * frequency / rate)
    result = complex(engine.gain[ch])
    for c, only in zip(engine.c, engine.only):
        if not only or only == ch + 1: result *= (c.b0 + c.b1*z + c.b2*z*z)/(1 + c.a1*z + c.a2*z*z)
    return abs(result)

def process(lib, d, values, channels=1):
    audio = (C.c_float * len(values))(*values)
    lib.peq_process(C.byref(d), audio, len(values)//channels)
    return list(audio)

def dsp_check(lib):
    p = parse(lib, 'Preamp: -6 dB\nFilter: ON PK Fc 1000 Hz Gain 6 dB Q 1')
    # The manual and imported representation use exactly the same compiler.
    manual = Preset(); lib.peq_default(C.byref(manual))
    manual.count, manual.bypass, manual.preamp = 1, 0, -6
    manual.bands[0] = Band(1, 0, 1000, 6, 1)
    for rate in (8000, 11025, 16000, 22050, 32000, 44100, 48000, 88200, 96000, 176400, 192000, 352800, 384000):
        a, b = Engine(), Engine()
        assert lib.peq_compile(C.byref(p), rate, C.byref(a))
        assert lib.peq_compile(C.byref(manual), rate, C.byref(b))
        assert bytes(a) == bytes(b)
        assert abs(20 * math.log10(response(a, rate, 1000))) < 1e-7
        d = DSP(); lib.peq_reset(C.byref(d), rate, 1, C.byref(p))
        samples = [0.05*math.sin(2*math.pi*1000*i/rate) for i in range(rate//5)]
        out = process(lib, d, samples)
        gain = math.sqrt(sum(x*x for x in out[rate//10:])/sum(x*x for x in samples[rate//10:]))
        assert abs(20*math.log10(gain)) < .01, (rate, gain)
    # Every band, both shelf endpoints, gain/Q limits and low-rate fallback.
    for index in range(10):
        p = Preset(); lib.peq_default(C.byref(p)); p.bypass = 0
        p.bands[index].enabled = 1; p.bands[index].gain = -6
        e = Engine(); assert lib.peq_compile(C.byref(p), 48000, C.byref(e))
        assert abs(20*math.log10(response(e, 48000, p.bands[index].frequency)) + 6) < 1e-7
    for kind in (1, 2):
        for q in (0.1, 1/math.sqrt(2), 10):
            for gain in (-24, 24):
                p.bands[9] = Band(1, kind, 1000, gain, q)
                e = Engine(); assert lib.peq_compile(C.byref(p), 48000, C.byref(e))
                assert abs(20*math.log10(response(e, 48000, 0 if kind == 1 else 24000))-gain) < 1e-7
                for c in e.c:
                    assert abs(c.a2) < 1 and 1+c.a1+c.a2 > 0 and 1-c.a1+c.a2 > 0
    p.bands[9] = Band(1, 0, 16000, 6, 1)
    e = Engine(); assert lib.peq_compile(C.byref(p), 8000, C.byref(e)) and not e.bypass
    assert (e.c[9].b0, e.c[9].a1) == (1, 0)  # only the unrepresentable band is skipped
    p = parse(lib, '\n'.join(f'Filter: ON PK Fc {100+i*1000} Hz Gain 6 dB Q 1' for i in range(10)))
    d = DSP(); lib.peq_reset(C.byref(d), 48000, 2, C.byref(p))
    clipped = process(lib, d, [1., -1.] * 20000, 2)
    assert all(math.isfinite(x) and -1 <= x <= 1 for x in clipped)
    # Block splitting and stereo independence; no file I/O or allocation in process.
    a, b = DSP(), DSP()
    for d in (a, b): lib.peq_reset(C.byref(d), 48000, 2, C.byref(p))
    signal = [x for i in range(10000) for x in (0.02*math.sin(i*.13), 0.)]
    entire = process(lib, a, signal, 2)
    split = process(lib, b, signal[:2346], 2) + process(lib, b, signal[2346:], 2)
    assert entire == split and all(x == 0 for x in entire[1::2])
    p.bypass = 1
    assert lib.peq_update(C.byref(a), C.byref(p))
    process(lib, a, [0., 0.] * 960, 2)
    assert process(lib, a, signal, 2) == list((C.c_float * len(signal))(*signal))
    # A settled bypass preserves every bit, even NaNs, signed zero and out-of-range input.
    bits = (C.c_uint32 * 6)(0x7fc00001, 0x80000000, 0x7f800000, 0xff800000, 0x40000000, 0xc0000000)
    before = bytes(bits), bytes(a)
    lib.peq_process(C.byref(a), C.cast(bits, C.POINTER(C.c_float)), 3)
    assert (bytes(bits), bytes(a)) == before
    # Rapid updates queue the latest target without restarting the current fade.
    p.bypass = 0
    assert lib.peq_update(C.byref(a), C.byref(p))
    process(lib, a, [0., 0.] * 100, 2)
    old_ramp = a.ramp
    p.preamp = -24
    assert lib.peq_update(C.byref(a), C.byref(p)) and a.ramp == old_ramp
    process(lib, a, [0., 0.] * 2000, 2)
    assert not a.ramp and not a.waiting and abs(a.current.gain[0]-10**(-24/20)) < 1e-12
    lib.peq_reset(C.byref(a), 96000, 1, C.byref(p))
    assert process(lib, a, [0.] * 1000) == [0.] * 1000
    # Decaying tails settle at a ~-590 dB normal-float residue, never in the denormal range.
    tail = process(lib, a, [0.5] + [0.] * 96000)
    state = [abs(a.current.z[0][i][j]) for i in range(30) for j in range(2)]
    assert all(z < 1e-28 and (z == 0 or z > 1e-300) for z in state) and abs(tail[-1]) < 1e-28
    # A preamp-only change carries filter memory: the crossfade is a pure gain ramp.
    lib.peq_reset(C.byref(a), 48000, 1, C.byref(p))
    tone = [0.1*math.sin(2*math.pi*100*i/48000) for i in range(20000)]
    process(lib, a, tone[:10000])
    p.preamp = -30; assert lib.peq_update(C.byref(a), C.byref(p))
    after = process(lib, a, tone[10000:])
    b = DSP(); p.preamp = -24; lib.peq_reset(C.byref(b), 48000, 1, C.byref(p))
    reference = process(lib, b, tone)[10000:]
    fade = [1 - (1 - 10**(-6/20)) * min(i + 1, 960) / 960 for i in range(10000)]
    assert max(abs(x - r*f) for x, r, f in zip(after, reference, fade)) < 1e-6
    # A band switched off mid-play drains its carried memory before it is skipped, then passes x through.
    p = parse(lib, 'Filter: ON PK Fc 100 Hz Gain 6 dB Q 1')
    lib.peq_reset(C.byref(a), 48000, 1, C.byref(p))
    process(lib, a, tone[:10000])
    p.bands[0].gain = 0; assert lib.peq_update(C.byref(a), C.byref(p))
    process(lib, a, tone[:2000])
    assert all(a.current.z[0][i][j] == 0 for i in range(30) for j in range(2)) and a.current.used == 1
    assert process(lib, a, tone) == list((C.c_float * len(tone))(*tone))
    # One-channel bands and balance: left gets the band and 2 dB less, right neither.
    p = parse(lib, 'Channel: L\nFilter: ON PK Fc 1000 Hz Gain 6 dB Q 1'); p.balance = 2
    e = Engine(); assert lib.peq_compile(C.byref(p), 48000, C.byref(e))
    assert abs(20*math.log10(response(e, 48000, 1000, 0)) - 4) < 1e-7
    assert abs(20*math.log10(response(e, 48000, 1000, 1))) < 1e-12
    lib.peq_reset(C.byref(a), 48000, 2, C.byref(p))
    out = process(lib, a, [x for v in tone for x in (v, v)], 2)
    assert out[1::2] == list((C.c_float * len(tone))(*tone))
    gain = math.sqrt(sum(x*x for x in out[10000::2])/sum(x*x for x in tone[5000:]))
    assert abs(20*math.log10(gain) + 2) < .1, gain  # 100 Hz: the band barely reaches it
    # A both-channel band narrowed to the left drops the right's carried memory.
    p.bands[0].enabled = 1; lib.peq_reset(C.byref(a), 48000, 2, C.byref(p)); process(lib, a, [.5, .5] * 100, 2)
    p.bands[0].enabled = 2; assert lib.peq_update(C.byref(a), C.byref(p)); process(lib, a, [0., 0.] * 2000, 2)
    assert a.current.z[1][0][0] == a.current.z[1][0][1] == 0 and a.current.z[0][0][0]
    print('PEQ DSP: C PCM response, 30 bands, shelves, rates, bypass, clipping, channels, balance and updates passed.')

# Host stand-ins for the stock services peq_platform.h maps on the device.
SHIM_H = r"""
#include <dirent.h>
#include <sys/stat.h>
typedef int (*handler)(void *, void *);
extern volatile unsigned char g_equalizer_flag, g_backlight_status;
""" + ''.join(f'{r} {n}({a});\n' for n in """
list_item_create label_create list_view_create scroll_view_create widget_use_style
widget_set_text_utf8 widget_on widget_get_prop_int widget_set_prop_int widget_destroy_children
widget_resize scroll_view_set_offset widget_invalidate_force timer_add timer_remove
navigator_back write_int_config widget_factory widget_factory_create_widget widget_set_prop_str
widget_get_text_utf8 widget_set_focused widget_lookup window_manager pages_set_active_by_name
canvas_set_fill_color canvas_fill_rect canvas_get_vgcanvas vgcanvas_save vgcanvas_restore vgcanvas_translate
vgcanvas_begin_path vgcanvas_close_path vgcanvas_move_to vgcanvas_line_to vgcanvas_arc vgcanvas_fill vgcanvas_stroke
vgcanvas_set_fill_color vgcanvas_set_stroke_color vgcanvas_set_fill_linear_gradient vgcanvas_set_line_width
widget_to_local on_wm_keyup_fun
""".split() for r, a in [PROTOTYPES[n]]) + r"""
"""
SHIM = r"""
static struct { int parent, h, bg; char text[160]; handler click, destroy, keyup, change, focus, down, move, up; void *ctx; } w[4096];
static int count = 1, timers, removed, backs;
static int (*timer_fn)(const void *);
volatile unsigned char g_equalizer_flag, g_backlight_status = 1;
int stock_eq_trampoline(int mode) { return mode; }
static void *make(void *parent, int h) {
    ++count; w[count].parent = (int)(long)parent; w[count].h = h;
    w[count].text[0] = 0; w[count].click = w[count].change = w[count].focus = w[count].down = 0; w[count].bg = 0; return (void *)(long)count;
}
/* The value menu's edit: its text, the keyboard it asks for, whether it took focus. */
static int edit_widget, focused;
static char keyboard[32];
void *widget_factory(void) { return 0; }
void *widget_factory_create_widget(void *f, const char *type, void *p, int x, int y, int ww, int h) {
    void *e = make(p, h);
    if (!strcmp(type, "edit")) { edit_widget = (int)(long)e; keyboard[0] = 0; focused = 0; }
    return e;
}
/* The curve's vgcanvas drawing (peq_paint) is checked on the device; here it only has to link. Filled
   rectangles land in screen and captions in texts, for 3EQ's captures. */
static unsigned fill_color, screen[290 * 375];
static struct { int r[4]; unsigned px, color, n, codepoints[16]; char s[65]; } texts[64];
static int ntexts;
int canvas_set_fill_color(void *c, unsigned v) { fill_color = v; return 0; }
int canvas_fill_rect(void *c, int x, int y, int ww, int h) {
    for (int j = y < 0 ? 0 : y; j < y + h && j < 290; ++j)
        for (int i = x < 0 ? 0 : x; i < x + ww && i < 375; ++i) screen[j * 375 + i] = fill_color;
    return 0;
}
void *canvas_get_vgcanvas(void *c) { return 0; }
#define VG(name, ...) int name(void *vg, ##__VA_ARGS__) { return 0; }
VG(vgcanvas_save) VG(vgcanvas_restore) VG(vgcanvas_begin_path) VG(vgcanvas_close_path) VG(vgcanvas_fill) VG(vgcanvas_stroke)
int vgcanvas_translate(void *vg, float x, float y) { return 0; }
int vgcanvas_move_to(void *vg, float x, float y) { return 0; }
int vgcanvas_line_to(void *vg, float x, float y) { return 0; }
int vgcanvas_arc(void *vg, float x, float y, float r, float a, float b, int ccw) { return 0; }
int vgcanvas_set_fill_color(void *vg, unsigned c) { return 0; }
int vgcanvas_set_stroke_color(void *vg, unsigned c) { return 0; }
int vgcanvas_set_line_width(void *vg, float w) { return 0; }
int vgcanvas_set_fill_linear_gradient(void *vg, float a, float b, float c, float d, unsigned e, unsigned f) {
    return 0;
}
void draw_centred(void *canvas, const unsigned *s, unsigned n, const void *r, unsigned px, unsigned color) {
    if (ntexts == 64) return;
    memcpy(texts[ntexts].r, r, sizeof texts[ntexts].r);
    texts[ntexts].px = px, texts[ntexts].color = color;
    if (n > 16) abort(); /* caption's fixed Unicode buffer, not a byte count */
    texts[ntexts].n = n;
    memcpy(texts[ntexts].codepoints, s, n * sizeof *s);
    unsigned at = 0;
    for (unsigned i = 0; i < n; ++i) {
        unsigned c = s[i];
        if (c < 0x80) texts[ntexts].s[at++] = (char)c;
        else {
            unsigned extra = c < 0x800 ? 1 : c < 0x10000 ? 2 : 3;
            texts[ntexts].s[at++] = (char)((extra == 1 ? 0xc0 : extra == 2 ? 0xe0 : 0xf0) | (c >> (6 * extra)));
            for (unsigned j = extra; j; --j) texts[ntexts].s[at++] = (char)(0x80 | ((c >> (6 * (j - 1))) & 63));
        }
    }
    texts[ntexts].s[at] = 0;
    ++ntexts;
}
/* iPod builds' theme: Classic's Graphite light tone, or Minimal's fill. */
static int shim_classic_on = 1;
int ipod_classic(void) { return shim_classic_on; }
unsigned accent_tone(int tone) { return shim_classic_on ? 0x6e6e6e : 0xeeeeec; }
void shim_classic(int on) { shim_classic_on = on; }
int widget_set_prop_str(void *x, const char *k, const char *v) {
    if ((long)x == edit_widget && !strcmp(k, "keyboard")) snprintf(keyboard, sizeof(keyboard), "%s", v);
    return 0;
}
int widget_get_text_utf8(void *x, char *s, unsigned size) {
    s[0] = 0;
    if (strlen(w[(long)x].text) < size) snprintf(s, size, "%s", w[(long)x].text);
    return 0;
}
int widget_set_focused(void *x, int v) {
    if ((long)x == edit_widget && (focused = v) && w[edit_widget].focus) w[edit_widget].focus(0, 0);
    return 0;
}
/* The stock keyboard window (4000) and its page panel (4001); the page it was switched to. */
static char page[16];
void *window_manager(void) { return (void *)3999L; }
void *widget_lookup(void *x, const char *name, int recursive) {
    if ((long)x == 3999 && !strcmp(name, "kb_default_t9")) return (void *)4000L;
    return (long)x == 4000 && !strcmp(name, "panel") ? (void *)4001L : 0;
}
int pages_set_active_by_name(void *x, const char *name) { if ((long)x == 4001) snprintf(page, sizeof(page), "%s", name); return 0; }
const char *shim_page(void) { return page; }
const char *shim_edit(void) { return edit_widget ? w[edit_widget].text : ""; }
const char *shim_keyboard(void) { return keyboard; }
int shim_focused(void) { return focused; }
/* Typing a value and closing the keyboard: the edit reports EVT_VALUE_CHANGED. */
void shim_run(void);
void shim_type(const char *text) {
    snprintf(w[edit_widget].text, 160, "%s", text);
    w[edit_widget].change(0, 0); shim_run();
}
void *list_item_create(void *p, int x, int y, int ww, int h) { return make(p, h); }
void *label_create(void *p, int x, int y, int ww, int h) { return make(p, h); }
void *list_view_create(void *p, int x, int y, int ww, int h) { return make(p, h); }
void *scroll_view_create(void *p, int x, int y, int ww, int h) { return make(p, h); }
int widget_use_style(void *x, const char *s) { return 0; }
int widget_set_text_utf8(void *x, const char *s) { snprintf(w[(long)x].text, 160, "%s", s); return 0; }
unsigned widget_on(void *x, unsigned type, handler f, void *ctx) {
    long i = (long)x;
    if (type == 0x10c) { w[i].click = f; w[i].ctx = ctx; }
    else if (type == 0x0c) w[i].destroy = f;
    else if (type == 0x0e) w[i].change = f;
    else if (type == 0x10e) w[i].focus = f;
    else if (type == 0x100) w[i].down = f;
    else if (type == 0x102) w[i].move = f;
    else if (type == 0x104) w[i].up = f;
    else w[i].keyup = f;
    return 1;
}
static int selection, offset;
int widget_get_prop_int(void *x, const char *k, int d) {
    return (long)x == 1 && !strcmp(k, "h") ? 290 : !strcmp(k, "_ringnav_index") ? selection : d;
}
int widget_set_prop_int(void *x, const char *k, int v) {
    if (!strcmp(k, "style:normal:bg_color")) w[(long)x].bg = v;
    if (!strcmp(k, "_ringnav_index")) selection = v;
    return 0;
}
int widget_destroy_children(void *x) { count = 1; return 0; }
int widget_resize(void *x, int ww, int h) { w[(long)x].h = h; return 0; }
int scroll_view_set_offset(void *x, int a, int b) { offset = b; return 0; }
int shim_selection(void) { return selection; }
int shim_offset(void) { return offset; }
int widget_invalidate_force(void *x, void *y) { return 0; }
unsigned timer_add(int (*f)(const void *), void *ctx, unsigned ms) { timer_fn = f; return ++timers; }
int timer_remove(unsigned id) { timer_fn = 0; ++removed; return 0; }
int navigator_back(void) { return ++backs; }
static int eqflag = -1;
int write_int_config(int v, const char *s, const char *k) { if (!strcmp(s, "PLAYSET") && !strcmp(k, "EQFLAG")) eqflag = v; return 0; }
int shim_eqflag(void) { return eqflag; }

int peq_page_init(void *page, void *context);
int shim_open(void) { count = 1; w[1].destroy = w[1].keyup = 0; return peq_page_init((void *)1, 0); }
int shim_pending(void) { return timer_fn != 0; }
int shim_removed(void) { return removed; }
unsigned char shim_flag(void) { return g_equalizer_flag; }
void shim_run(void) { int (*f)(const void *) = timer_fn; timer_fn = 0; if (f) f(0); }
/* Click the row whose labels, caption and value joined by a space, start with text; optionally let
 * the deferred render run (1), or only find it (2). */
int shim_click(const char *text, int run) {
    for (int p = 2; p <= count; ++p) {
        char joined[400] = "";
        for (int i = p + 1; i <= count; ++i)
            if (w[i].parent == p) snprintf(joined + strlen(joined), sizeof(joined) - strlen(joined), "%s%s", *joined ? " " : "", w[i].text);
        if (w[p].click && *joined && !strncmp(joined, text, strlen(text))) {
            if (run == 2) return 1; /* only look */
            w[p].click(w[p].ctx, 0);
            if (run) shim_run();
            return 1;
        }
    }
    return 0;
}
void shim_return(void) { int event[8] = {0}; event[6] = 170; w[1].keyup(0, event); shim_run(); }
void shim_close(void) { w[1].destroy(0, 0); }
const char *shim_title(void) {
    for (int i = 2; i <= count; ++i) if (w[i].parent == 1 && w[i].h == 48) return w[i].text;
    return "";
}
int shim_list_height(void) { return w[2].h; } /* the list view is the page's first child */
/* The Simple tab: its view the page's child with a press handler, the tab bar the 48px one. Centre's
 * single press runs at once; a double press is the screen's. */
#include "offsets.inc"
static int screen_off;
static int child(int bar) {
    for (int i = count; i > 1; --i) if (w[i].parent == 1 && w[i].down && (w[i].h == 48) == bar) return i;
    return 0;
}
void stop_timer(unsigned *t) { if (*t) timer_remove(*t); *t = 0; }
int center_press(unsigned *timer, unsigned *at, int (*single)(const void *), int scrubbing) {
    single(0); return 1;
}
int on_wm_keyup_fun(void *a, void *b) { return ++screen_off; }
int widget_to_local(void *x, void *xy) { return 0; }
int eq_key(void *top, unsigned key);
int shim_e3_open(void) { return shim_open(); } /* the EQ page opens on Simple */
int shim_e3_key(int k) { return eq_key((void *)1, (unsigned[]){KEY_PREV, KEY_NEXT, KEY_CENTER}[k]); }
void shim_e3_return(void) { int event[8] = {0}; event[EVENT_KEY / 4] = KEY_RETURN; w[1].keyup(0, event); }
void shim_e3_close(void) { w[1].destroy(0, 0); }
int shim_e3_touch(int type, int x, int y) {
    int event[8] = {0}, view = child(0);
    event[EVENT_TYPE / 4] = type; event[EVENT_X / 4] = x; event[EVENT_Y / 4] = y;
    handler f = type == EVT_POINTER_DOWN ? w[view].down : type == EVT_POINTER_UP_BEFORE ? w[1].up : w[1].move;
    return f ? f(0, event) : -1;
}
/* A tap on the tab bar at x, and the render it asks for. */
int shim_tab_touch(int x) {
    int event[8] = {0}, bar = child(1);
    event[EVENT_X / 4] = x;
    if (!bar) return 0;
    w[bar].down(0, event); shim_run(); return 1;
}
const char *shim_e3_title(void) {
    for (int i = count; i > 1; --i) if (w[i].parent == 1 && w[i].h == 48) return w[i].text;
    return "";
}
int shim_timers(void) { return timers; }
/* 3EQ's page painted black first, as the window's background is; then its rectangles and captions. */
void peq_paint(void *w, void *canvas);
const unsigned *shim_e3_paint(void) {
    static char canvas[0x100], lcd[0x100];
    *(void **)(canvas + CANVAS_LCD) = lcd;
    for (int i = 0; i < 290 * 375; ++i) screen[i] = 0xff000000u;
    ntexts = 0;
    peq_paint((void *)(long)child(0), canvas);
    peq_paint((void *)(long)child(1), canvas); /* the tabs, at the page's origin as the view is */
    return screen;
}
void caption(void *, const char *, int, int, int, int, unsigned, unsigned);
unsigned shim_caption(const char *s, unsigned *out) {
    ntexts = 0;
    caption(0, s, 1, 2, 3, 4, 20, 0xffffffffu);
    if (ntexts != 1) abort();
    memcpy(out, texts[0].codepoints, texts[0].n * sizeof *out);
    return texts[0].n;
}
int shim_texts(void) { return ntexts; }
const char *shim_text(int i, int *r, unsigned *px, unsigned *color) {
    memcpy(r, texts[i].r, sizeof texts[i].r); *px = texts[i].px; *color = texts[i].color; return texts[i].s;
}
int shim_backs(void) { return backs; }
int shim_screen_off(void) { return screen_off; }
unsigned shim_list_bg(void) { return (unsigned)w[2].bg; }
"""

def caption_check(ui):
    """Real caption decoding: Unicode scalars, malformed bytes and the 16-code-point cap."""
    ui.shim_caption.argtypes = [C.c_char_p, C.POINTER(C.c_uint)]
    ui.shim_caption.restype = C.c_uint
    def check(data, want):
        out = (C.c_uint * 18)(*([0xdeadbeef] * 18))
        n = ui.shim_caption(data, C.cast(C.byref(out, C.sizeof(C.c_uint)), C.POINTER(C.c_uint)))
        assert n == len(want) and list(out)[1:1+n] == want, (data, n, list(out))
        assert out[0] == out[17] == 0xdeadbeef
        assert all(v == 0xdeadbeef for v in list(out)[1+n:17])
    for text in ('', 'ASCII 1k +3 dB', '简单：开启', '频谱', 'aé中😀', '中' * 16, '中' * 17,
                 'a' * 15 + '中😀', '\u0080\u07ff\u0800\ud7ff\ue000\uffff\U00010000\U0010ffff'):
        check(text.encode(), list(map(ord, text[:16])))
    for bad in (b'\x80', b'\xc0', b'\xc1', b'\xf5', b'\xff', b'\xc2', b'\xe4\xb8', b'\xf0\x9f\x98',
                b'\xc0\xaf', b'\xe0\x80\xaf', b'\xed\xa0\x80', b'\xf0\x80\x80\xaf', b'\xf4\x90\x80\x80'):
        # Invalid sequences consume one byte at a time; an ASCII suffix remains intact.
        check(bad, [0xfffd] * len(bad))
        check(bad + b'A', [0xfffd] * len(bad) + [ord('A')])
    check(b'\xe4A', [0xfffd, ord('A')])
    check(b'a' * 16 + b'\xf0', [ord('a')] * 16)
    check(b'A\0' + '中'.encode(), [ord('A')])
    print('Caption: Chinese and mixed UTF-8, invalid/truncated scalars, NUL and 16-code-point boundaries passed.')

def editor_check(lib, tmp):
    """The editor's promises, driven through its real click and render paths."""
    (tmp/'shim.h').write_text(SHIM_H)
    (tmp/'shim.c').write_text('#include "peq.h"\n' + SHIM)
    ui = compile_host(tmp, 'peq_ui.so', ROOT/'patch/peq_ui.c', ROOT/'patch/peq.c', tmp/'shim.c',
                      '-I', ROOT/'patch', '-include', tmp/'shim.h')
    ui.shim_title.restype = C.c_char_p
    caption_check(ui)
    data, saved = tmp/'root/mnt/data', tmp/'root/mnt/data/peq-presets'
    saved.mkdir(parents=True); (tmp/'root/mnt/mmc/EQ').mkdir(parents=True)
    active = data/'peq-active'
    def preset(**band):
        p = Preset(); lib.peq_default(C.byref(p)); p.bypass = 0
        for k, v in band.items(): setattr(p.bands[0], k, v)
        return p
    def read():  # what plays: the PEQ, bypassed unless it is the mode
        p = Preset(); assert lib.peq_load_active(C.byref(p)); return p
    def click(text): assert ui.shim_click(text.encode(), 1), (text, ui.shim_title())
    title = lambda: ui.shim_title().decode()
    def advanced():  # the EQ page opens on Simple; its tab bar's right half is Advanced
        r = ui.shim_open(); assert ui.shim_tab_touch(300); return r
    named = lambda name: ui.shim_click('预设 '.encode() + name.encode(), 2)  # the draft's preset, on its row

    assert lib.peq_save(bytes(active), C.byref(preset(enabled=1)), 1) == 1
    # With the stock flag clear no filter runs, so the saved ON reads OFF.
    assert advanced() == 0 and title() == ''
    # The visualizer's attach puts the filter in the chain with PEQ off: the saved ON becomes OFF, so it stays silent.
    ui.shim_close(); ui.peq_attach(); assert read().bypass == 1 and ui.shim_flag() == 0; advanced()
    assert lib.peq_save(bytes(active), C.byref(preset(enabled=1)), 1) == 1
    # The switch writes the stock config key, so boot restores it; the flag follows the preset.
    click('高级 关闭'); assert ui.shim_eqflag() == 1 and ui.shim_flag() == 1
    click('高级 打开'); assert ui.shim_eqflag() == 0 and ui.shim_flag() == 0
    ui.shim_close(); assert advanced() == 0; click('高级 关闭')
    full = ui.shim_list_height()
    # iPod rows are transparent, so the list itself must paint black, not the theme's light card.
    ui.shim_list_bg.restype = C.c_uint; assert ui.shim_list_bg() == 0xff000000
    # Bypass switches at once but keeps unapplied band edits out of the active preset.
    click('1  '); click('增益 +0.0 dB'); assert title() == '频段 1 增益'; click('+1.0 dB'); ui.shim_return()
    click('高级 打开')
    assert read().bypass == 1 and read().bands[0].gain == 0 and ui.shim_flag() == 0
    click('应用更改')
    assert read().bands[0].gain == 1 and read().bypass == 1 and title() == '已应用'
    # A band edit sets the preamp to minus the combined response's peak: here the one +1 dB band.
    assert abs(read().preamp + 1) < 0.01, read().preamp
    # A message takes the title bar; the list keeps every row.
    assert ui.shim_list_height() == full == 144  # 3 rows under the curve
    # Loading into the editor does not activate it.
    assert lib.peq_save(bytes(saved/'HD650.peq'), C.byref(preset(enabled=1, gain=-3.0)), 1) == 1
    before = active.read_bytes()
    click('预设'); click('HD650.peq')
    assert active.read_bytes() == before and title() == '预设已载入，请选择应用以启用'
    click('高级 关闭'); click('高级 打开')  # any action clears the message
    assert named('HD650') and read().bands[0].gain == 1  # the draft's name; still the applied preset
    # A failed apply or switch leaves the active preset untouched.
    before = active.read_bytes()
    (data/'peq-active.tmp').mkdir()
    click('应用更改')
    assert title() == '应用失败，当前均衡器未变' and active.read_bytes() == before
    click('高级 关闭')
    assert title() == '切换失败，均衡器未变' and active.read_bytes() == before
    (data/'peq-active.tmp').rmdir()
    click('应用更改'); assert read().bands[0].gain == -3
    # Closing cancels the pending render.
    assert ui.shim_click('预设'.encode(), 0) and ui.shim_pending()
    removed = ui.shim_removed()
    ui.shim_close()
    assert not ui.shim_pending() and ui.shim_removed() == removed + 1
    advanced()
    assert named('HD650')  # the applied preset's name survives the page
    # Deleting asks first, removes only the saved copy and leaves the active EQ alone.
    before = active.read_bytes()
    click('预设'); click('删除预设'); click('HD650.peq'); click('取消')
    assert (saved/'HD650.peq').exists()
    click('HD650.peq'); click('删除 HD650.peq？确认')
    assert not (saved/'HD650.peq').exists() and active.read_bytes() == before
    assert title() == '已删除 HD650.peq，当前均衡器未变' and not ui.shim_click(b'HD650.peq', 0)
    # Stock presets are built in: picking one loads stock's curve, writes nothing until Apply.
    files, before = sorted(saved.iterdir()), active.read_bytes()
    ui.shim_return(); click('内置预设'); click('摇滚')
    assert title() == '预设已载入，请选择应用以启用' and active.read_bytes() == before
    assert sorted(saved.iterdir()) == files
    click('应用更改'); p = read()
    assert [p.bands[k].gain for k in range(10)] == [-2, 0, 2, 4, -2, -2, 0, 0, 4, 4]
    assert [p.bands[k].enabled for k in range(10)] == [1, 0, 1, 1, 1, 1, 0, 0, 1, 1] and p.preamp < -4
    # Overlapping boosts add up (+6.5 and +6 dB at 1 kHz); cuts alone leave 0 dB.
    p = preset(enabled=1, gain=6.0); p.count = 2; p.bands[1] = p.bands[0]; p.preamp = -1
    assert lib.peq_save(bytes(active), C.byref(p), 1) == 1
    ui.shim_close(); advanced(); click('1  '); click('增益 +6.0 dB'); click('+6.5 dB'); ui.shim_return(); click('应用更改')
    assert read().preamp == -1, read().preamp  # a preamp off Auto (here the preset's) sticks through band edits
    click('前级增益 -1.0 dB'); assert title() == '前级增益' and ui.shim_selection() == 27  # opens on -1.0
    click('自动 (-12.5 dB)'); click('应用更改')
    assert abs(read().preamp + 12.5) < 0.05, read().preamp
    click('1  '); click('频段 打开'); ui.shim_return(); click('2  '); click('频段 打开'); ui.shim_return(); click('应用更改')
    assert math.copysign(1, read().preamp) == 1 and read().preamp == 0, read().preamp  # +0: shown as 0.0
    # Channels cycle on enabled bands; headroom takes the louder side, balance only turns one down.
    click('1  峰值 31 Hz 关闭'); click('增益 +6.5 dB'); click('+7.5 dB'); click('声道 双声道')  # picking a gain turns the band on
    assert title() == '频段 1'
    click('声道 左声道'); ui.shim_return()
    # Balance one row opens a picker, L 12 to R 12 dB in 0.5 dB steps, on the current value.
    click('声道平衡 居中'); assert title() == '声道平衡'
    assert (ui.shim_selection(), ui.shim_offset()) == (24, 24 * 48 - 96)  # 5 rows of 48 shown
    click('R 12.0 dB'); assert title() == ''; click('声道平衡 R 12.0 dB')
    assert (ui.shim_selection(), ui.shim_offset()) == (48, 49 * 48 - 240)  # clamped to the end
    ui.shim_return(); assert title() == ''; click('声道平衡 R 12.0 dB'); click('L 0.5 dB'); click('应用更改')
    r = read(); assert (r.bands[0].enabled, r.balance) == (3, -0.5) and abs(r.preamp + 7.5) < 0.05, (r.bands[0].enabled, r.balance)
    assert ui.shim_click('1  峰值 31 Hz R +7.5 dB'.encode(), 0) and ui.shim_click('声道平衡 L 0.5 dB'.encode(), 0)
    # Frequency and Q open a value menu: an edit with the value (keyboard on tap or centre), then Raise/Lower.
    ui.shim_edit.restype = ui.shim_keyboard.restype = C.c_char_p
    edit = lambda: ui.shim_edit().decode()
    ui.shim_close(); advanced(); click('1  峰值 31 Hz R'); click('频率 31 Hz')
    assert title() == '频段 1 频率 (Hz)' and edit() == '31' and ui.shim_keyboard() == b'kb_default_t9'
    click('减少 1 Hz'); click('减少 1 Hz'); assert edit() == '29'
    click('步长 1 Hz'); click('减少 10 Hz'); assert edit() == '20'  # clamped at 20 Hz
    click('步长 10 Hz'); click('增加 100 Hz'); assert edit() == '120'
    ui.shim_page.restype = C.c_char_p
    assert not ui.shim_focused(); click('120'); assert ui.shim_focused()  # the centre button opens the keyboard
    assert ui.shim_page() == b'symnum'  # on its number keys
    ui.shim_type(b'2500'); assert edit() == '2500'
    ui.shim_type(b'abc'); assert edit() == '2500'      # not a number: unchanged
    ui.shim_type('\u0131'.encode()); assert edit() == '2500'  # low wchar byte is ASCII '1', still not a number
    ui.shim_type(b'9'*16); assert edit() == '2500'    # stock conversion refuses a value that cannot fit
    ui.shim_type(b'99999'); assert edit() == '20000'   # clamped like Raise
    ui.shim_type(b'1000.5'); assert edit() == '1000'   # kept exactly, shown whole
    ui.shim_return(); assert title() == '频段 1'
    click('Q 1.41'); assert title() == '频段 1 Q' and edit() == '1.41'
    click('增加 0.05'); assert edit() == '1.46'
    ui.shim_type(b'0.7'); assert edit() == '0.70'
    ui.shim_return(); ui.shim_return(); click('应用更改')
    r = read(); assert (r.bands[0].frequency, round(r.bands[0].q, 9)) == (1000.5, 0.7), (r.bands[0].frequency, r.bands[0].q)
    print('EQ Advanced tab: bypass, apply, auto preamp, channels, balance, frequency and Q, load, failed saves, delete and close passed.')
    eq3_editor_check(lib, ui, tmp)

def eq3_check(lib, tmp):
    """3EQ's filters on the shared engine, and the one state record PEQ and 3EQ share."""
    def eq3(low, mid, high):
        p = Preset(); lib.eq3_bands(C.byref(p), C.byref((C.c_double * 3)(low, mid, high))); return p
    def db(p, f, rate=48000):
        e = Engine(); assert lib.peq_compile(C.byref(p), rate, C.byref(e)); return 20*math.log10(response(e, rate, f))
    p = eq3(6, -6, 6)
    assert [(b.enabled, b.type, b.frequency, b.q) for b in p.bands[:3]] == [(1, 1, 155, .707), (1, 0, 900, .707), (1, 2, 5000, .707)]
    assert p.count == 3 and p.bypass == 0 and p.preamp == 0 and lib.peq_valid(C.byref(p))
    assert eq3(0, 6, 0).bands[1].frequency == eq3(0, 0, 0).bands[1].frequency == 1750  # Mid boosts higher than it cuts
    # Shelves reach their gain at the ends and leave the other end alone; Mid peaks at its own frequency.
    for g in (-6, -0.5, 0.5, 6):
        assert abs(db(eq3(g, 0, 0), 20) - g) < .15 and abs(db(eq3(g, 0, 0), 20000)) < .01
        assert abs(db(eq3(0, 0, g), 20000) - g) < .15 and abs(db(eq3(0, 0, g), 20)) < .01
        f = 900 if g < 0 else 1750
        assert abs(db(eq3(0, g, 0), f) - g) < 1e-7 and abs(db(eq3(0, g, 0), 20)) < .01
        grid = [20 * 1000 ** (k / 400) for k in range(401)]
        peak = max(grid, key=lambda x: g * db(eq3(0, g, 0), x))
        assert abs(math.log2(peak / f)) < .05, (g, peak)
    # Flat is identity: no band runs, and the PCM passes unchanged while 3EQ is on.
    e = Engine(); assert lib.peq_compile(C.byref(eq3(0, 0, 0)), 48000, C.byref(e)) and e.used == 0 and not e.bypass
    d = DSP(); lib.peq_reset(C.byref(d), 48000, 2, C.byref(eq3(0, 0, 0)))
    signal = [0.7*math.sin(i*.031) for i in range(4000)]
    assert process(lib, d, signal, 2) == list((C.c_float * len(signal))(*signal))
    # Finite, stable coefficients at every supported rate; a shelf above Nyquist is skipped.
    for rate in (8000, 11025, 22050, 44100, 48000, 88200, 96000, 176400, 192000, 352800, 384000):
        for gains in ((6, -6, 6), (-6, 6, -6)):
            e = Engine(); assert lib.peq_compile(C.byref(eq3(*gains)), rate, C.byref(e))
            for c in e.c:
                assert all(math.isfinite(getattr(c, k)) for k in ('b0', 'b1', 'b2', 'a1', 'a2'))
                assert abs(c.a2) < 1 and 1+c.a1+c.a2 > 0 and 1-c.a1+c.a2 > 0
        assert (e.c[2].b0, e.c[2].a1) == ((1, 0) if rate <= 10000 else (e.c[2].b0, e.c[2].a1))
    # Mid crossing zero changes frequency through the engine's 20 ms crossfade: no step in the output.
    d = DSP(); lib.peq_reset(C.byref(d), 48000, 1, C.byref(eq3(0, -6, 0)))
    tone = [0.25*math.sin(2*math.pi*1300*i/48000) for i in range(9600)]
    out = process(lib, d, tone[:4800])
    assert lib.peq_update(C.byref(d), C.byref(eq3(0, 6, 0)))
    out += process(lib, d, tone[4800:])
    step = max(abs(a - b) for a, b in zip(out[1:], out))
    assert step < 2.2 * 0.25 * 2 * math.pi * 1300 / 48000, step  # at most twice the tone's own slope
    # The state: one Q2PEQ03 record. None yet: Off, PEQ's defaults, 3EQ flat.
    active, s = tmp/'root/mnt/data/peq-active', State()
    active.parent.mkdir(parents=True, exist_ok=True)
    if active.exists(): active.unlink()
    assert not lib.eq_load(C.byref(s)) and (s.mode, s.hold) == (EQ_OFF, 0) and s.peq.bypass == 1
    assert bytes(s.eq3) == bytes(eq3(0, 0, 0))
    # Upgrade: a Q2PEQ02 (or 01) preset keeps its bands, on unless it was bypassed; 3EQ starts flat and off.
    q = parse(lib, 'Preamp: -3 dB\nFilter: ON PK Fc 1000 Hz Gain 3 dB Q 1')
    for bypass, mode in ((0, EQ_PEQ), (1, EQ_OFF)):
        q.bypass = bypass; assert lib.peq_save(bytes(active), C.byref(q), 1) == 1
        assert lib.eq_load(C.byref(s)) and s.mode == mode and bytes(s.peq) == bytes(q) and bytes(s.eq3) == bytes(eq3(0, 0, 0))
    active.write_bytes(b'Q2PEQ01\0' + bytes(q)[:8 + 10 * 32 + 8])
    assert lib.eq_load(C.byref(s)) and s.mode == EQ_OFF and s.peq.count == 1
    # Saved whole, read back as saved; the effective preset follows the mode.
    s.eq3 = eq3(3, -2, 1); s.eq3.preamp = -3; s.mode = EQ_3
    assert lib.eq_save(C.byref(s)) and active.read_bytes()[:8] == b'Q2PEQ03\0' and len(active.read_bytes()) == 8 + C.sizeof(State)
    t, e = State(), Preset()
    assert lib.eq_load(C.byref(t)) and bytes(t) == bytes(s)
    assert lib.peq_load_active(C.byref(e)) and bytes(e) == bytes(s.eq3)
    for mode, hold, want in ((EQ_PEQ, 0, s.peq), (EQ_OFF, 0, s.peq), (EQ_OFF, 1, None)):
        t.mode, t.hold = mode, hold; lib.eq_effective(C.byref(t), C.byref(e))
        if want is None:  # 3EQ's Off on its page: the preamp alone, no bands
            assert (e.bypass, e.count, e.preamp, e.balance) == (0, 0, -3, 0)
        else:
            assert e.bypass == (mode == EQ_OFF) and bytes(e.bands) == bytes(want.bands) and e.preamp == want.preamp
    # A corrupt, truncated or overlong record reads as no state; a failed write keeps the last one.
    good = active.read_bytes()
    for bad in (good[:-1], good + b'\0', b'Q2PEQ09' + good[7:], good[:8] + b'\7' + good[9:], b''):
        active.write_bytes(bad); assert not lib.eq_load(C.byref(t)) and t.mode == EQ_OFF and t.peq.bypass == 1
    active.write_bytes(good)
    (active.parent/'peq-active.tmp').mkdir()
    s.mode = EQ_PEQ; assert not lib.eq_save(C.byref(s)) and active.read_bytes() == good
    (active.parent/'peq-active.tmp').rmdir()
    s.mode = 3; assert not lib.eq_save(C.byref(s)) and active.read_bytes() == good  # never an invalid record
    active.unlink()
    print('3EQ: filters, asymmetric Mid, identity, rates, smooth updates, state upgrade, effective preset, corrupt and failed writes passed.')

def eq3_captures(tmp, out, font):
    """EQ's Simple tab in the iPod build's two themes, as e3_paint and tabs_paint draw it (rectangles exact, captions set in the
    stock font at their sizes, centred in their boxes): illustrations for review, not AWTK captures."""
    from ipod import imagemagick
    (tmp/'shim.h').write_text(SHIM_H)
    (tmp/'shim.c').write_text('#include "peq.h"\n' + SHIM)
    ui = compile_host(tmp, 'peq_ui_ipod.so', ROOT/'patch/peq_ui.c', ROOT/'patch/peq.c', tmp/'shim.c',
                      '-I', ROOT/'patch', '-include', tmp/'shim.h', '-DIPOD=1')
    ui.shim_e3_paint.restype = C.POINTER(C.c_uint32 * (290 * 375))
    ui.shim_text.restype = C.c_char_p
    (tmp/'root/mnt/data').mkdir(parents=True)
    def shot(name):
        rgba = bytes(ui.shim_e3_paint().contents)
        args = ['-size', '375x290', '-depth', '8', 'rgba:-']
        for i in range(ui.shim_texts()):
            r, px, color = (C.c_int * 4)(), C.c_uint(), C.c_uint()
            text, c = ui.shim_text(i, r, C.byref(px), C.byref(color)).decode(), color.value
            args += ['(', '-size', f'{r[2]}x{r[3]}', 'xc:none', '-font', str(font), '-pointsize', str(px.value),
                     '-fill', f'#{c & 255:02x}{c >> 8 & 255:02x}{c >> 16 & 255:02x}', '-gravity', 'center',
                     '-annotate', '+0+0', text, ')', '-gravity', 'NorthWest', '-geometry', f'+{r[0]}+{r[1]}', '-composite']
        (out/f'3eq-{name}.png').write_bytes(imagemagick(*args, '-alpha', 'off', 'png:-', data=rgba))
    PREV, NEXT, CENTRE = range(3)
    for classic, theme in ((1, 'classic'), (0, 'minimal')):
        ui.shim_classic(classic)
        (tmp/'root/mnt/data/peq-active').unlink(missing_ok=True)
        ui.peq_attach()  # EQ off, the flag clear, 3EQ flat
        ui.shim_e3_open()
        keys = lambda *k: [ui.shim_e3_key(x) for x in k]
        keys(CENTRE, *[NEXT] * 6, CENTRE, NEXT, CENTRE, *[PREV] * 4, CENTRE, NEXT, CENTRE, *[NEXT] * 8, CENTRE, PREV, PREV)
        shot(f'{theme}-off')  # Low +3, Mid -2, High +4; Low selected; 3EQ off
        keys(PREV, CENTRE, NEXT, NEXT, CENTRE, PREV)
        shot(f'{theme}-on-adjusting')  # 3EQ on, Mid being turned to -2.5 dB
        keys(CENTRE, NEXT, NEXT)
        shot(f'{theme}-on-reset-selected')
        keys(*[PREV] * 5)
        shot(f'{theme}-tabs-selected')
        ui.shim_e3_close()

def eq3_editor_check(lib, ui, tmp):
    """3EQ's page through its real keys, touch and timers, against the state PEQ shares."""
    active = tmp/'root/mnt/data/peq-active'
    ui.shim_e3_title.restype = C.c_char_p
    title = lambda: ui.shim_e3_title().decode()
    PREV, NEXT, CENTRE = range(3)
    def state():
        s = State(); assert lib.eq_load(C.byref(s)); return s
    def gains(s=None): return [b.gain for b in (s or state()).eq3.bands[:3]]
    def keys(*k):
        for x in k: assert ui.shim_e3_key(x)
    # PEQ playing; opening 3EQ changes nothing.
    ui.shim_close(); ui.shim_open(); assert ui.shim_tab_touch(300)
    if ui.shim_flag() == 0: assert ui.shim_click('高级 关闭'.encode(), 1)
    ui.shim_close()
    before = active.read_bytes(); peq = bytes(state().peq)
    assert state().mode == EQ_PEQ and ui.shim_flag() == 1
    assert ui.shim_e3_open() == 0 and title() == '' and active.read_bytes() == before
    # The wheel picks Mid (from Low), Centre takes it, the wheel turns it in 0.5 dB steps: one save
    # timer for the run, and nothing written until it fires. 3EQ stays off; PEQ keeps playing.
    timers = ui.shim_timers()
    keys(NEXT, CENTRE, NEXT, NEXT, NEXT, NEXT)
    assert ui.shim_timers() == timers + 1 and active.read_bytes() == before and ui.shim_pending()
    ui.shim_run(); s = state()
    assert gains(s) == [0, 2, 0] and s.eq3.bands[1].frequency == 1750 and s.mode == EQ_PEQ and bytes(s.peq) == peq
    assert abs(s.eq3.preamp + 2) < .05 and ui.shim_flag() == 1  # headroom for the boost
    # Below zero Mid moves to 900 Hz; Centre ends the adjustment and saves at once.
    keys(*[PREV] * 7); assert gains() == [0, 2, 0]
    keys(CENTRE); s = state(); assert not ui.shim_pending() and gains(s) == [0, -1.5, 0] and s.eq3.bands[1].frequency == 900
    assert s.eq3.preamp == 0  # cuts need no headroom
    keys(NEXT); assert gains() == [0, -1.5, 0]  # the wheel moves the selection again: High
    # Return ends the adjustment without leaving; the limits hold.
    backs = ui.shim_backs(); keys(CENTRE, *[NEXT] * 20); ui.shim_e3_return()
    assert gains() == [0, -1.5, 6] and ui.shim_backs() == backs and title() == ''
    # The switch: 3EQ on turns PEQ off and keeps it; the indicator stays on.
    keys(PREV, PREV, PREV, CENTRE)
    s = state(); assert s.mode == EQ_3 and bytes(s.peq) == peq and ui.shim_flag() == 1 and title() == ''
    e = Preset(); assert lib.peq_load_active(C.byref(e)) and bytes(e) == bytes(s.eq3) and e.bypass == 0
    # While on, adjustments are heard as they are saved (here at the Centre that ends one).
    keys(NEXT, CENTRE, NEXT, CENTRE); assert gains() == [0.5, -1.5, 6] and state().mode == EQ_3
    # Off from here keeps the 3EQ's preamp playing until the page closes; the indicator goes off.
    keys(PREV, CENTRE); s = state()
    assert (s.mode, s.hold) == (EQ_OFF, 1) and ui.shim_flag() == 0 and ui.shim_eqflag() == 0
    assert lib.peq_load_active(C.byref(e)) and (e.bypass, e.count) == (0, 0) and e.preamp == s.eq3.preamp < 0
    keys(CENTRE); s = state(); assert (s.mode, s.hold) == (EQ_3, 0) and ui.shim_flag() == 1 and ui.shim_eqflag() == 1
    # Reset zeroes the gains and keeps the mode.
    keys(*[NEXT] * 4, CENTRE); s = state(); assert gains(s) == [0, 0, 0] and s.mode == EQ_3 and s.eq3.preamp == 0
    # Touch: a press on a slider takes it and sets the gain under the finger; the drag follows in
    # 0.5 dB steps; lifting saves. The page is 290 high: travel 138..230, 0 dB at 184.
    assert ui.shim_e3_touch(0x100, 62, 138) == 0 and ui.shim_e3_touch(0x102, 62, 100) == 0
    assert gains() == [0, 0, 0] and ui.shim_pending()
    ui.shim_e3_touch(0x102, 187, 176); ui.shim_e3_touch(0x104, 187, 176)  # the press chose Low; x no longer matters
    assert gains() == [1, 0, 0] and not ui.shim_pending()
    ui.shim_e3_touch(0x100, 312, 230); ui.shim_e3_touch(0x104, 312, 230); assert gains() == [1, 0, -6]
    ui.shim_e3_touch(0x102, 312, 138); assert gains() == [1, 0, -6]  # no drag after the lift
    ui.shim_e3_touch(0x100, 300, 70); assert gains() == [0, 0, 0]  # Reset
    ui.shim_e3_touch(0x100, 50, 70); assert state().mode == EQ_OFF  # the switch
    ui.shim_e3_touch(0x100, 50, 70); assert state().mode == EQ_3
    # A failed save says so and keeps the last good record; the next one writes the pending gains.
    good = active.read_bytes(); (active.parent/'peq-active.tmp').mkdir()
    keys(NEXT, CENTRE, NEXT, CENTRE)
    assert title() == '保存失败，均衡器未变' and active.read_bytes() == good
    ui.shim_tab_touch(300)
    assert title() == '保存失败，均衡器未变' and active.read_bytes() == good
    (active.parent/'peq-active.tmp').rmdir()
    keys(CENTRE, CENTRE); assert gains() == [0.5, 0, 0] and title() == ''
    # Return leaves; leaving with Off held lets the preamp go.
    keys(PREV, CENTRE); assert state().hold == 1
    backs = ui.shim_backs(); ui.shim_e3_return(); assert ui.shim_backs() == backs + 1
    ui.shim_e3_close(); s = state(); assert (s.mode, s.hold) == (EQ_OFF, 0)
    assert lib.peq_load_active(C.byref(e)) and e.bypass == 1 and ui.shim_flag() == 0
    # PEQ's switch selects PEQ again; the 3EQ settings stay.
    ui.shim_open(); assert ui.shim_tab_touch(300) and ui.shim_click('高级 关闭'.encode(), 1); s = state()
    assert s.mode == EQ_PEQ and bytes(s.peq) == peq and gains(s) == [0.5, 0, 0] and ui.shim_flag() == 1
    ui.shim_e3_open(); assert ui.shim_e3_touch(0x100, 50, 70) == 0 and state().mode == EQ_3  # and back
    ui.shim_e3_close(); assert state().mode == EQ_3 and ui.shim_flag() == 1
    # The tabs: the wheel reaches the bar above Simple's switch, Centre opens Advanced with the wheel
    # still on the bar; past it the list's first row, and back. Neither tab changes what plays.
    before = active.read_bytes()
    ui.shim_e3_open(); keys(PREV, PREV, CENTRE); ui.shim_run()
    assert ui.shim_click('高级 关闭'.encode(), 2) and ui.shim_selection() == -1 and title() == ''
    keys(NEXT); assert ui.shim_selection() == 0
    assert not ui.shim_e3_key(NEXT)  # the list's own
    keys(PREV); assert ui.shim_selection() == -1
    keys(CENTRE); ui.shim_run(); assert not ui.shim_click('高级'.encode(), 2)  # Simple: no list
    keys(NEXT, CENTRE); assert state().mode == EQ_OFF; keys(CENTRE)  # the switch, from the bar
    assert active.read_bytes() == before and state().mode == EQ_3
    ui.shim_e3_close()
    print('EQ Simple tab: entry without change, tabs, wheel and touch adjustment, coalesced saves, switch, held Off, reset, failed save and indicator passed.')

# Drives patch/peq_player.c the way hciplayer's af chain does. Built 32-bit like the device,
# so the file's ABI asserts hold; checked against the shared DSP driven directly.
PLAYER = r"""
#include <assert.h>
#include "peq.h"
int mp3_toc(const unsigned char *h, unsigned n, double t, double *frac, double *length);
int peq_open(af_instance *af);

char *peq_mpctx; /* the player's MPContext pointer; null: no codec to check */
double peq_pts = -1; /* written_audio_pts: seconds decoded */
int peq_length;      /* {media-info}'s whole seconds */
static unsigned clock_reads;
int __real_clock_gettime(clockid_t clock, struct timespec *t);
int __wrap_clock_gettime(clockid_t clock, struct timespec *t) {
    ++clock_reads;
    return __real_clock_gettime(clock, t);
}
static int left = -1; /* calloc calls before one fails; -1: never */
void *test_calloc(size_t n, size_t size) {
    if (!left--) return 0;
    void *p = malloc(n * size);
    return p ? memset(p, 0, n * size) : p;
}

static peq_preset active(double preamp, double gain) {
    peq_preset p;
    peq_default(&p);
    p.bypass = 0; p.preamp = preamp; p.count = 1;
    p.bands[0] = (peq_band){1, 0, 1000, gain, 1};
    assert(peq_save(PEQ_ACTIVE, &p, 1) == 1);
    return p;
}

static int negotiate(af_instance *af, af_data *in) { return af->control(af, 0x10000100, in); }

/* The visualizer's tap (demo creates the file before the player maps it). */
static vis_tap *tap_view(void) {
    static vis_tap *t;
    if (!t) {
        int fd = open(VIS_FILE, O_RDWR | O_CREAT, 0644);
        assert(fd >= 0 && !ftruncate(fd, sizeof(vis_tap)));
        t = mmap(0, sizeof(vis_tap), PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
        assert(t != MAP_FAILED && !close(fd));
    }
    return t;
}

/* One block through the filter equals the same block through the reference DSP, and is filtered. */
static void same(af_instance *af, peq_dsp *ref, int rate, int nch) {
    float a[1024], b[1024], in[1024];
    unsigned frames = 1024 / nch;
    for (int i = 0; i < 1024; ++i) a[i] = b[i] = in[i] = 0.5f * sinf(i * 0.05f);
    af_data d = {a, (int)(frames * nch * 4), rate, nch, 0x1d, 4};
    ++tap_view()->want; /* the visualizer is watching */
    assert(af->play(af, &d) == &d);
    peq_process(ref, b, frames);
    assert(!memcmp(a, b, sizeof(a)) && memcmp(a, in, sizeof(a)));
    /* The tap ends with what played: the first two channels, every rate / 44100th frame above 48 kHz. */
    vis_tap *t = tap_view();
    unsigned step = rate > 48000 ? (unsigned)rate / 44100 : 1, n = (frames + step - 1) / step;
    assert(t->rate == (unsigned)rate / step && t->stamp > 0);
    for (unsigned i = 0; i < n; ++i)
        for (int ch = 0; ch < 2; ++ch)
            assert(t->ring[(t->seq - n + i) % VIS_RING][ch] == a[i * step * nch + (ch && nch > 1)]);
}

/* Crossfade (peq_player.c crossfade): demo's flag, one filter instance per track. */
static volatile int *xfade_view(int ms) {
    static volatile int *f;
    if (!f) {
        int fd = open(XFADE_FILE, O_RDWR | O_CREAT, 0644);
        assert(fd >= 0 && !ftruncate(fd, sizeof *f));
        f = mmap(0, sizeof *f, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
        assert(f != MAP_FAILED && !close(fd));
    }
    *f = ms;
    return f;
}
static af_instance *track(af_instance *af, int rate) {
    memset(af, 0, sizeof(*af));
    assert(peq_open(af) == 1);
    af_data in = {0, 0, rate, 2, 0x1d, 4};
    assert(negotiate(af, &in) == 1);
    return af;
}
/* One stereo block: channel 0 a ramp from *n (the track's frame count), channel 1 level. Returns
 * the frames put out, into out. */
static unsigned block(af_instance *af, float *out, unsigned frames, unsigned *n, float level, int rate) {
    for (unsigned i = 0; i < frames; ++i) out[2 * i] = (float)(*n + i), out[2 * i + 1] = level;
    *n += frames;
    peq_pts = (double)*n / rate;
    af_data d = {out, (int)(frames * 8), rate, 2, 0x1d, 4};
    assert(af->play(af, &d) == &d && d.audio == out && d.len % 8 == 0);
    return (unsigned)d.len / 8;
}

static void crossfade_check(void) {
    static float buf[2 * 1024];
    af_instance a, b;
    unlink(PEQ_ACTIVE); /* the defaults: bypass, so the filter leaves samples exact */
    xfade_view(1000);
    peq_length = 10;
    /* Outgoing: untouched until the hold window (2 x 1 s + 2 s before the end), then half of each
     * block until a second is held; what comes out is the track in order, nothing skipped. */
    track(&a, 48000);
    unsigned n = 0, out = 0, held;
    while (n < 10 * 48000) {
        unsigned k = block(&a, buf, 1024, &n, 1, 48000);
        if (n <= 6 * 48000) assert(k == 1024);
        for (unsigned i = 0; i < k; ++i) assert(buf[2 * i] == (float)out++);
    }
    held = n - out;
    assert(held == 48000);
    a.uninit(&a);
    /* Incoming: its first second mixed with the held tail, equal power (ch 1: 1 out, 0 in, so the
     * tail's cos), the tail's frames in order; then the track alone. Every frame comes out. */
    track(&b, 48000);
    xfade_view(0); /* disabling the next fade must still finish the already held tail */
    unsigned m = 0, mixed = 0;
    while (m < 2 * 48000) {
        unsigned start = m, k = block(&b, buf, 1024, &m, 0, 48000);
        assert(k == 1024);
        for (unsigned i = 0; i < k; ++i, ++mixed) {
            if (mixed < held) {
                float p = (mixed + 0.5f) / held * 1.5707963f;
                assert(fabsf(buf[2 * i + 1] - cosf(p)) < 1e-5f);
                assert(fabsf(buf[2 * i] - ((start + i) * sinf(p) + (out + mixed) * cosf(p))) < 0.5f);
            } else
                assert(buf[2 * i] == (float)(start + i) && buf[2 * i + 1] == 0);
        }
    }
    /* Halfway the two are at equal power: cos^2 + sin^2 = 1, each at 0.707. */
    float p = (held / 2 + 0.5f) / held * 1.5707963f;
    assert(fabsf(cosf(p) - 0.70710678f) < 1e-3f);
    b.uninit(&b);

    /* No fade asked (another album next, Crossfade off, Gapless off): every block whole. */
    xfade_view(0);
    track(&a, 48000);
    /* Let the unwatched visualizer tap settle before counting clock queries. */
    for (n = 0; n < 2 * 48000;) assert(block(&a, buf, 1024, &n, 1, 48000) == 1024);
    unsigned reads = clock_reads;
    for (n = 0; n < 10 * 48000;) assert(block(&a, buf, 1024, &n, 1, 48000) == 1024);
    assert(clock_reads == reads);
    a.uninit(&a);

    /* A seek while holding drops the held audio (it was left behind) and plays on whole. */
    xfade_view(1000);
    track(&a, 48000);
    for (n = 0; n < 9 * 48000;) block(&a, buf, 1024, &n, 1, 48000);
    n = 2 * 48000; /* back to 2 s */
    assert(block(&a, buf, 1024, &n, 1, 48000) == 1024 && buf[0] == 2 * 48000);
    a.uninit(&a);
    /* A tail followed by another rate is dropped: the next track plays alone. */
    track(&a, 48000);
    for (n = 0; n < 10 * 48000;) block(&a, buf, 1024, &n, 1, 48000);
    a.uninit(&a);
    track(&b, 44100);
    m = 0;
    assert(block(&b, buf, 1024, &m, 0, 44100) == 1024 && buf[0] == 0 && buf[1] == 0);
    b.uninit(&b);
    /* The same instance starting over at the end (a chain kept across the change) is the next track. */
    track(&a, 48000);
    for (n = 0; n < 10 * 48000;) block(&a, buf, 1024, &n, 1, 48000);
    peq_length = 30; /* the next track's, once the player has switched */
    m = 0;
    for (mixed = 0; m < 2 * 48000;) {
        unsigned start = m, k = block(&a, buf, 1024, &m, 0, 48000);
        assert(k == 1024);
        for (unsigned i = 0; i < k; ++i, ++mixed)
            if (mixed < 48000) assert(fabsf(buf[2 * i + 1] - cosf((mixed + 0.5f) / 48000 * 1.5707963f)) < 1e-5f);
            else assert(buf[2 * i] == (float)(start + i) && buf[2 * i + 1] == 0);
    }
    a.uninit(&a);
    peq_length = 10;
    /* Unknown length or position: no hold. */
    peq_length = 0;
    track(&a, 48000);
    for (n = 0; n < 10 * 48000;) assert(block(&a, buf, 1024, &n, 1, 48000) == 1024);
    a.uninit(&a);
    /* A 10 s fade: the hold wraps the ring; odd block sizes; order and the equal-power gain hold. */
    xfade_view(10000);
    peq_length = 60;
    static const unsigned sizes[] = {1024, 333, 17, 1000};
    unsigned j = 0;
    track(&a, 48000);
    for (n = out = 0; n < 60 * 48000;) {
        unsigned k = block(&a, buf, sizes[j++ % 4], &n, 1, 48000);
        for (unsigned i = 0; i < k; ++i) assert(buf[2 * i] == (float)out++);
    }
    held = n - out;
    a.uninit(&a);
    track(&b, 48000);
    for (m = mixed = 0; mixed < held;) {
        unsigned k = block(&b, buf, sizes[j++ % 4], &m, 0, 48000);
        for (unsigned i = 0; i < k && mixed < held; ++i, ++mixed)
            assert(fabsf(buf[2 * i + 1] - cosf((mixed + 0.5f) / held * 1.5707963f)) < 1e-5f);
    }
    b.uninit(&b);
    peq_length = 10;
    xfade_view(0);
    peq_pts = -1;
}

int main(void) {
    af_instance af;
    tap_view();
    /* A failed open is cleaned up by af_create calling uninit on the partial state. */
    for (int n = 0; n < 2; ++n) {
        memset(&af, 0, sizeof(af));
        left = n;
        assert(peq_open(&af) == -2);
        af.uninit(&af);
        assert(!af.data && !af.setup);
    }
    left = -1;
    memset(&af, 0, sizeof(af));
    assert(peq_open(&af) == 1 && af.mul == 1 && !af.delay && af.data && af.setup);
    player_state *s = af.setup;

    /* Format negotiation: bad channel counts fail, unsupported rates and formats pass through as they
       come (never detach: af_add would hand back the freed filter), other formats are asked to convert
       to float, and float is accepted. */
    af_data in = {0, 0, 48000, 0, 0x1d, 4};
    assert(negotiate(&af, 0) == -2);
    assert(negotiate(&af, &in) == -2);
    in.nch = PEQ_CHANNELS + 1; assert(negotiate(&af, &in) == -2);
    in.nch = 2; in.rate = 7999; assert(negotiate(&af, &in) == 1 && af.data->rate == 7999);
    in.rate = 384001; assert(negotiate(&af, &in) == 1 && af.data->rate == 384001);
    in.rate = 48000; in.format = 64; assert(negotiate(&af, &in) == 1 && af.data->format == 64);
    /* DSD passes through untouched, by the vendor's dsdiff driver or an FFmpeg ffdsd codec; other codecs negotiate. */
    static char *mp[12], *sh[2], *codec[0x3e4 / 4];
    peq_mpctx = (char *)mp; mp[0x2c / 4] = (char *)sh; sh[1] = (char *)codec;
    in.format = 0x1d;
    float dop[4] = {0.5f, -0.5f, 0.25f, -0.25f};
    af_data pass = {dop, sizeof(dop), 48000, 2, 0x1d, 4};
    codec[0x3e0 / 4] = "dsdiff"; assert(negotiate(&af, &in) == 1 && !memcmp(af.data, &in, sizeof(in)));
    codec[0x3e0 / 4] = "ffmpeg"; codec[0x3d0 / 4] = "ffdsdmsbf"; assert(negotiate(&af, &in) == 1);
    codec[0x3d0 / 4] = "ffflac"; assert(negotiate(&af, &in) == 1 && s->dsp.channels == 2);
    codec[0x3e0 / 4] = "dsdiff"; assert(negotiate(&af, &in) == 1); /* after PCM, as a reinit would */
    assert(af.play(&af, &pass) == &pass && pass.len == sizeof(dop) && dop[0] == 0.5f && dop[3] == -0.25f);
    peq_mpctx = 0;
    peq_preset first = active(-6, 6);
    in.format = 0x11; in.bps = 2;
    assert(negotiate(&af, &in) == 0 && in.format == 0x1d && in.bps == 4);
    assert(in.rate == 48000 && in.nch == 2);
    assert(negotiate(&af, &in) == 1);
    assert(af.data->format == 0x1d && af.data->bps == 4);
    assert(af.data->rate == 48000 && af.data->nch == 2);
    assert(s->dsp.rate == 48000 && s->dsp.channels == 2);
    assert(!memcmp(&s->preset, &first, sizeof(first)));
    peq_dsp ref;
    peq_reset(&ref, 48000, 2, &first);
    same(&af, &ref, 48000, 2);

    /* Buffers not in the negotiated format pass through untouched and leave the state alone. */
    float a[64] = {0.25f}, keep[64];
    memcpy(keep, a, sizeof(a));
    af_data bad[] = {
        {a, 256, 44100, 2, 0x1d, 4}, {a, 256, 48000, 1, 0x1d, 4}, {a, 256, 48000, 2, 0x11, 4},
        {a, 256, 48000, 2, 0x1d, 2}, {a, 252, 48000, 2, 0x1d, 4}, {a, 0, 48000, 2, 0x1d, 4},
    };
    for (unsigned i = 0; i < sizeof(bad) / sizeof(bad[0]); ++i)
        assert(af.play(&af, &bad[i]) == &bad[i] && !memcmp(a, keep, sizeof(a)));
    assert(af.play(&af, 0) == 0);
    same(&af, &ref, 48000, 2);

    /* Live updates arrive through the stock gain query on the playback loop, channel 0 only. */
    float gains[10]; /* stock's graphic EQ array */
    struct { float *gain; int channel; } ext = {gains, 1};
    peq_preset second = active(-3, -4);
    assert(af.control(&af, 0x40001d00, &ext) == 1 && !memcmp(&s->preset, &first, sizeof(first)));
    same(&af, &ref, 48000, 2);
    ext.channel = 0;
    assert(af.control(&af, 0x40001d00, &ext) == 1 && !memcmp(&s->preset, &second, sizeof(second)));
    assert(peq_update(&ref, &second));
    for (int i = 0; i < 4; ++i) same(&af, &ref, 48000, 2); /* through the crossfade */
    /* An unchanged or unreadable active file keeps the current filter. */
    assert(af.control(&af, 0x40001d00, &ext) == 1);
    FILE *f = fopen(PEQ_ACTIVE, "wb");
    assert(f && fputs("junk", f) >= 0 && !fclose(f));
    assert(af.control(&af, 0x40001d00, &ext) == 1 && !memcmp(&s->preset, &second, sizeof(second)));
    same(&af, &ref, 48000, 2);
    /* The stock graphic gains read back as flat; malformed queries fail. */
    for (int i = 0; i < 10; ++i) gains[i] = 1;
    assert(af.control(&af, 0x40001d01, &ext) == 1);
    for (int i = 0; i < 10; ++i) assert(gains[i] == 0);
    assert(af.control(&af, 0x40001d00, 0) == -2);
    ext.channel = PEQ_CHANNELS; assert(af.control(&af, 0x40001d00, &ext) == -2);
    ext.channel = -1; assert(af.control(&af, 0x40001d00, &ext) == -2);
    ext.channel = 0; ext.gain = 0; assert(af.control(&af, 0x40001d00, &ext) == -2);
    assert(af.control(&af, 0x20000300, 0) == 1 && af.control(&af, 0x12345, 0) == -1);

    /* A new track renegotiates: new rate and channels, the active preset reread, fresh state. */
    peq_preset third = active(0, 3);
    in = (af_data){0, 0, 96000, 1, 0x1d, 4};
    assert(negotiate(&af, &in) == 1 && s->dsp.rate == 96000 && s->dsp.channels == 1);
    assert(!memcmp(&s->preset, &third, sizeof(third)));
    peq_reset(&ref, 96000, 1, &third);
    same(&af, &ref, 96000, 1);
    memcpy(a, keep, sizeof(a));
    af_data old = {a, 256, 48000, 2, 0x1d, 4};
    assert(af.play(&af, &old) == &old && !memcmp(a, keep, sizeof(a)));
    /* A 3EQ state record: the live update plays the 3EQ, then Off's held preamp alone, then nothing. */
    eq_state st;
    double g[3] = {3, -2, 1};
    assert(eq_load(&st) && st.mode == EQ_PEQ);
    eq3_bands(&st.eq3, g);
    st.eq3.preamp = -3, st.mode = EQ_3;
    ext.gain = gains;
    assert(eq_save(&st) && af.control(&af, 0x40001d00, &ext) == 1 && !memcmp(&s->preset, &st.eq3, sizeof(st.eq3)));
    st.mode = EQ_OFF, st.hold = 1;
    assert(eq_save(&st) && af.control(&af, 0x40001d00, &ext) == 1);
    assert(!s->preset.bypass && !s->preset.count && s->preset.preamp == -3);
    st.hold = 0;
    assert(eq_save(&st) && af.control(&af, 0x40001d00, &ext) == 1 && s->preset.bypass);
    /* Without an active file a track starts on the defaults. */
    assert(!unlink(PEQ_ACTIVE));
    in.rate = 44100;
    assert(negotiate(&af, &in) == 1);
    peq_preset defaults;
    peq_default(&defaults);
    assert(!memcmp(&s->preset, &defaults, sizeof(defaults)));

    /* With nobody watching for a second of audio, the tap stops copying; a bump resumes it. */
    vis_tap *t = tap_view();
    unsigned seq = t->seq;
    float block[2 * 1000] = {0};
    af_data idle = {block, (int)sizeof(block), 44100, 1, 0x1d, 4};
    for (int i = 0; i < 100; ++i) af.play(&af, &idle); /* 200000 frames, past 44100 */
    unsigned after = t->seq;
    assert(after != seq && after - seq < 50000);
    af.play(&af, &idle); assert(t->seq == after);
    ++t->want; af.play(&af, &idle); assert(t->seq == after + 2000);

    af.uninit(&af);
    assert(!af.data && !af.setup);
    crossfade_check();

    /* Exact VBR seeking: a 44.1 kHz stereo MPEG-1 Layer III Xing frame, 3600 s long, whose table
     * puts the first half of the time in the first quarter of the bytes. */
    unsigned char h[192] = {0xff, 0xfb, 0x90, 0x00};
    unsigned frames = 3600 * 44100 / 1152; /* 137812 frames, 3599.98 s */
    memcpy(h + 36, "Xing\0\0\0\x07", 8);
    h[44] = frames >> 24; h[45] = frames >> 16; h[46] = frames >> 8; h[47] = frames;
    for (int i = 0; i < 100; i++) h[52 + i] = i < 50 ? i * 64 / 50 : 64 + (i - 50) * 192 / 50;
    double frac, length;
    assert(mp3_toc(h, sizeof h, 0, &frac, &length) && frac == 0 && fabs(length - frames * 1152.0 / 44100) < 1e-9);
    assert(mp3_toc(h, sizeof h, length / 2, &frac, &length) && fabs(frac - 0.25) < 1e-9);
    assert(mp3_toc(h, sizeof h, length / 4, &frac, &length) && fabs(frac - 0.125) < 1e-9);
    assert(mp3_toc(h, sizeof h, length * 0.995, &frac, &length) && frac > 0.98 && frac < 1);
    assert(mp3_toc(h, sizeof h, length, &frac, &length) && frac == 1);
    assert(!mp3_toc(h, 100, 10, &frac, &length));        /* cut short */
    h[43] = 3;                                            /* no table */
    assert(!mp3_toc(h, sizeof h, 10, &frac, &length));
    h[43] = 7; memcpy(h + 36, "Info", 4);                 /* CBR: stock is already exact */
    assert(!mp3_toc(h, sizeof h, 10, &frac, &length));
    memcpy(h + 36, "Xing", 4); h[1] = 0xfd;               /* Layer III only */
    assert(!mp3_toc(h, sizeof h, 10, &frac, &length));
    h[1] = 0xf3; h[2] = 0x90;                             /* MPEG-2, 22.05 kHz: side info 17, 576 a frame */
    memmove(h + 21, h + 36, 120);
    assert(mp3_toc(h, sizeof h, 0, &frac, &length) && fabs(length - frames * 576.0 / 22050) < 1e-9);
    return 0;
}
"""

def player_check(tmp):
    """hciplayer's filter: negotiation, track changes, live updates and cleanup."""
    root = tmp/'player'
    (root/'mnt/data').mkdir(parents=True)
    (root/'tmp').mkdir()
    (tmp/'player_test.c').write_text(PLAYER)
    binary = tmp/'player_test'
    sources = [ROOT/'patch/peq_player.c', ROOT/'patch/peq.c', tmp/'player_test.c']
    subprocess.run(['cc', '-m32', '-DPEQ_HOST', f'-DPEQ_ROOT="{root}"', '-Dcalloc=test_calloc',
                    '-O2', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter', '-I', str(ROOT/'patch'),
                    *map(str, sources), '-Wl,--wrap=clock_gettime', '-lm', '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
    print('PEQ player: negotiation, pass-through, the visualizer tap, live updates, track changes, cleanup, crossfade and the Xing seek table passed.')

def visualizer_check(tmp):
    """The visualizer's analysis (patch/visualizer.c vis_bands): a sine lands in its band at full scale."""
    lib = compile_host(tmp, 'visualizer.so', ROOT/'patch/visualizer.c', ROOT/'patch/peq.c', '-DIPOD=1', '-I', ROOT/'patch')
    rate, n, bands = 44100, 1024, 64
    def levels(pcm):
        out = (C.c_float * bands)()
        lib.vis_bands((C.c_float * (2 * n))(*pcm), rate, out)
        return list(out)
    assert levels([0.0] * (2 * n)) == [0.0] * bands  # silence rests
    edge = lambda b: int(40 * 400 ** (b / bands) * n / rate + 0.5)  # the C band_bin
    for k in (12, 100, 300):  # a sine exactly on bin k, in both channels
        pcm = [math.sin(2 * math.pi * k * i / n) for i in range(n) for _ in (0, 1)]
        got, want = levels(pcm), max(b for b in range(bands) if edge(b) <= k)
        assert got[want] > 0.99 and got.index(max(got)) <= want, (k, want, got)
        assert all(v == 0 for b, v in enumerate(got) if edge(b) > k + 1 or edge(b + 1) < k - 1), (k, got)
        assert abs(levels([v / 2 for v in pcm])[want] - (1 - 6.0206 / 60)) < 0.01  # half scale: 6 dB down
    harness = tmp/'visualizer_checks.c'
    harness.write_text(r"""
#include <assert.h>
#include "visualizer.c"
void check_styles(void) {
    for (int style = 0; style < STYLES; ++style) {
        memset(&vz, 0, sizeof vz);
        for (int i = 0; i < VIS_N; ++i) {
            vz.pcm[i][0] = sinf(TAU * 12 * i / VIS_N);
            vz.pcm[i][1] = 0;
        }
        vz.re[0] = 123; /* FFT scratch stays untouched by time-domain styles. */
        vis_analyze(style, 44100, 0.04f, 100);
        if (style == SCOPE || style == METERS) {
            assert(vz.re[0] == 123 && vz.bin_rate == 0);
            for (int i = 0; i < VIS_HALO; ++i) assert(vz.level[i] == 0);
        } else {
            assert(vz.bin_rate == 44100 && vz.re[0] != 123);
            float max = 0;
            for (int i = 0; i < VIS_HALO; ++i) if (vz.level[i] > max) max = vz.level[i];
            assert(max > 0.5f);
        }
        if (style == SCOPE) assert(vz.wave[0][10] != 0 && vz.wave[1][10] == 0);
        else for (int ch = 0; ch < 2; ++ch)
            for (int i = 0; i < VIS_W; ++i) assert(vz.wave[ch][i] == 0);
        if (style == METERS) assert(vz.vu[0] > 0 && vz.vu[1] == 0 && vz.lit[0] == 500);
        else assert(vz.vu[0] == 0 && vz.vu[1] == 0);
        if (style != SPECTRUM)
            for (int i = 0; i < VIS_BARS; ++i) assert(vz.peak[i] == 0);
        vis_reset();
        for (int i = 0; i < VIS_HALO; ++i) assert(vz.level[i] == 0);
        for (int i = 0; i < VIS_BARS; ++i) assert(vz.peak[i] == 0);
        assert(vz.vu[0] == 0 && vz.lit[0] == 0 && vz.bass == 0 && vz.wave[0][10] == 0);
        vis_analyze(style, 44100, 0.04f, 100);
        for (unsigned now = 140; now <= 1600; now += 40) vis_analyze(style, 0, 0.04f, now);
        for (int i = 0; i < VIS_HALO; ++i) assert(vz.level[i] == 0);
        for (int i = 0; i < VIS_BARS; ++i) assert(vz.peak[i] == 0);
        for (int ch = 0; ch < 2; ++ch) {
            assert(vz.vu[ch] < 0.0001f);
            for (int i = 0; i < VIS_W; ++i) assert(fabsf(vz.wave[ch][i]) < 0.0003f);
        }
    }
}
""")
    checked = compile_host(tmp, 'visualizer_checks.so', harness, ROOT/'patch/peq.c', '-DIPOD=1', '-I', ROOT/'patch')
    checked.check_styles()
    print('Visualizer: FFT bands, selected-style work, stereo scope/VU and pause decay passed.')

SCROBBLE = r"""
#include <assert.h>
#include <stdarg.h>
#include <sys/stat.h>
#include "peq.h"
int scrobble_ready(void), scrobble_start(void), scrobble_poll(int *), scrobble_album_artist(void);
void scrobble_append(const char *, unsigned);

static const char *cfg[6]; /* TOKEN, USER, PASSWORD, API_KEY, API_SECRET, ALBUM_ARTIST */
int toolsReadConfig(const char *path, const char *section, const char *key, char *out, const char *def) {
    static const char *const keys[] = { "TOKEN", "USER", "PASSWORD", "API_KEY", "API_SECRET", "ALBUM_ARTIST" };
    static const char *const sections[] = { "LISTENBRAINZ", "LASTFM", "LASTFM", "LASTFM", "LASTFM", "SCROBBLE" };
    assert(strstr(path, "/mnt/mmc/.scrobble.ini"));
    for (int i = 0; i < 6; i++)
        if (!strcmp(key, keys[i]) && !strcmp(section, sections[i]) && cfg[i]) { strcpy(out, cfg[i]); return 1; }
    return -1; /* stock leaves out alone for a missing file or key */
}

/* libcurl: every request is dumped as url, headers, verify, body; replies are scripted. */
typedef unsigned (*writer)(const char *, unsigned, unsigned, void *);
typedef struct node { const char *s; struct node *next; } node;
static struct { const char *url, *body; node *h; writer w; void *ctx; long verify, code; } easy;
static int requests, fail_at = -1, no_key, append_at = -1;
static FILE *dump;
void *curl_easy_init(void) { memset(&easy, 0, sizeof easy); return &easy; }
void *curl_slist_append(void *list, const char *s) {
    node *n = calloc(1, sizeof *n), *l = list;
    n->s = s;
    if (!l) return n;
    while (l->next) l = l->next;
    l->next = n;
    return list;
}
void curl_slist_free_all(void *list) { for (node *n = list, *x; n; n = x) x = n->next, free(n); }
int curl_easy_setopt(void *c, int opt, ...) {
    va_list a;
    va_start(a, opt);
    assert(c == &easy);
    if (opt == 10002) easy.url = va_arg(a, const char *);
    else if (opt == 10015) easy.body = va_arg(a, const char *);
    else if (opt == 10023) easy.h = va_arg(a, node *);
    else if (opt == 20011) easy.w = va_arg(a, writer);
    else if (opt == 10001) easy.ctx = va_arg(a, void *);
    else if (opt == 64) { easy.verify = va_arg(a, long); assert(easy.verify == 1); }
    else if (opt == 81) assert(va_arg(a, long) == 2);
    else if (opt == 10065) assert(strstr(va_arg(a, const char *), "/etc/scrobble-ca.pem"));
    else assert(opt == 99 || opt == 13 || opt == 78 || opt == 81);
    va_end(a);
    return 0;
}
int curl_easy_perform(void *c) {
    int i = requests++;
    if (i == append_at) scrobble_append("New\tB\tListen\t\t100\tL\t1700009999\t\n", 33); /* a listen meanwhile */
    fprintf(dump, "%s\t", easy.url);
    for (node *n = easy.h; n; n = n->next) fprintf(dump, "%s|", n->s);
    fprintf(dump, "\t%ld\t%s\n", easy.verify, easy.body);
    const char *reply = !strstr(easy.url, "audioscrobbler") ? "{\"status\":\"ok\"}"
                        : strstr(easy.body, "auth.getMobileSession") ? (no_key ? "{\"session\":{}}" : "{\"session\":{\"name\":\"u\",\"key\":\"SK123\"}}")
                        : "{\"scrobbles\":{\"@attr\":{\"accepted\":1,\"ignored\":0}}}";
    easy.code = i == fail_at ? 500 : 200;
    easy.w(reply, 1, (unsigned)strlen(reply), easy.ctx);
    return 0;
}
int curl_easy_getinfo(void *c, int opt, ...) {
    va_list a;
    va_start(a, opt);
    assert(c == &easy && opt == 0x200002);
    *va_arg(a, long *) = easy.code;
    va_end(a);
    return 0;
}
void curl_easy_cleanup(void *c) { assert(c == &easy); }

static char *slurp(const char *path) {
    static char s[1 << 20];
    FILE *f = fopen(path, "rb");
    size_t n = f ? fread(s, 1, sizeof s - 1, f) : 0;
    if (f) fclose(f);
    s[n] = 0;
    return f ? s : 0;
}
static int lines(const char *s) { int n = 0; for (; s && *s; s++) n += *s == '\n'; return n; }
static int run(int *sent) {
    int r, started = scrobble_start();
    if (started <= 0) return started - 10;
    while (!(r = scrobble_poll(sent))) usleep(1000);
    assert(!scrobble_poll(sent)); /* reported once */
    return r;
}
static void write_log(int n) {
    FILE *f = fopen(ROOT "/mnt/mmc/.scrobbler.log", "wb");
    fputs("#AUDIOSCROBBLER/1.1\n#TZ/UTC\n#CLIENT/Q2 Pod\n", f);
    for (int i = 0; i < n; i++)
        fprintf(f, "Art \"%d\" \\ &=+%%\xc3\xa9\t%s\tTitle %d\t%d\t%d\tL\t%d\t\n", i, i % 10 ? "Alb/um" : "", i, i, 200 + i, 1700000000 + i);
    fputs("Skipped\tA\tT\t\t200\tS\t1700000000\t\njunk\n# a comment\n", f);
    fclose(f);
}
#define LOG ROOT "/mnt/mmc/.scrobbler.log"
#define HEADER "#AUDIOSCROBBLER/1.1\n#TZ/UTC\n#CLIENT/Q2 Pod\n"

int main(void) {
    int sent = -1;
    dump = fopen(ROOT "/requests", "w");
    setvbuf(dump, 0, _IONBF, 0);
    assert(!scrobble_ready() && run(&sent) == -11); /* no accounts: no row, and never an upload */
    assert(!scrobble_album_artist()); /* [SCROBBLE] ALBUM_ARTIST: off unless 1 */
    cfg[5] = "0";
    assert(!scrobble_album_artist());
    cfg[5] = "1";
    assert(scrobble_album_artist() && !scrobble_ready()); /* not an account */
    cfg[0] = "tok";
    assert(scrobble_ready() == 1);
    cfg[1] = "u", cfg[2] = "p&w", cfg[3] = "key";
    assert(scrobble_ready() == 1); /* Last.fm needs all four */
    cfg[4] = "sec";
    assert(scrobble_ready() == 3);
    assert(run(&sent) == 1 && !sent && !requests); /* no log: nothing to upload */

    write_log(120);
    append_at = 0;
    assert(run(&sent) == 1 && sent == 120 && requests == 7); /* 50, 50, 20 to each, one session */
    assert(!strcmp(slurp(LOG), HEADER "New\tB\tListen\t\t100\tL\t1700009999\t\n"));
    char *s = slurp(LOG ".sent");
    assert(lines(s) == 122 && !strstr(s, "#") && strstr(s, "junk\n"));

    write_log(60); /* ListenBrainz fails the second batch: the first leaves, the rest stays */
    append_at = -1, fail_at = 10;
    assert(run(&sent) == -1 && sent == 50);
    s = slurp(LOG);
    assert(!strncmp(s, HEADER "Art \"50\"", sizeof HEADER + 7) && lines(s) == 3 + 10 + 3);
    assert(lines(slurp(LOG ".sent")) == 172);

    char before[1 << 16];
    strcpy(before, slurp(LOG)); /* no session key: nothing changes */
    fail_at = -1, no_key = 1;
    assert(run(&sent) == -1 && !sent && !strcmp(slurp(LOG), before) && lines(slurp(LOG ".sent")) == 172);
    cfg[1] = 0, no_key = 0; /* An old card CA file is ignored: firmware supplies trust. */
    fclose(fopen(ROOT "/mnt/mmc/.scrobble.pem", "w"));
    assert(run(&sent) == 1 && sent == 10 && !strcmp(slurp(LOG), HEADER));
    /* A log rewrite that fails (a full card) keeps the listens to resend, and .sent gains none. */
    assert(!mkdir(LOG ".tmp", 0755));
    write_log(1); strcpy(before, slurp(LOG));
    assert(run(&sent) == -1 && sent == 1 && !strcmp(slurp(LOG), before) && lines(slurp(LOG ".sent")) == 184);
    rmdir(LOG ".tmp");
    /* A full archive must never discard the source listens after network success. */
    unlink(LOG ".sent"); assert(!symlink("/dev/full", LOG ".sent"));
    assert(run(&sent) == -1 && sent == 1 && !strcmp(slurp(LOG), before));
    unlink(LOG ".sent");
    fclose(dump);
    return 0;
}
"""

def scrobble_check(tmp):
    """Upload Scrobbles: config, log parsing and batching, the ListenBrainz JSON, the signed Last.fm
    form, and the log rewrite, on a real pthread with libcurl scripted and the host's libcrypto MD5."""
    import hashlib, json, urllib.parse
    root = tmp/'scrobble'
    (root/'mnt/mmc').mkdir(parents=True)
    shim = ['#include <pthread.h>', '#define pthread_mutex_lock(m) pthread_mutex_lock((pthread_mutex_t *)(m))',
            '#define pthread_mutex_unlock(m) pthread_mutex_unlock((pthread_mutex_t *)(m))', '#define tk_snprintf snprintf']
    shim += [f'{PROTOTYPES[n][0]} {n}({PROTOTYPES[n][1]});' for n in PROTOTYPES if n.startswith(('curl_', 'MD5_', 'toolsReadConfig'))]
    (tmp/'scrobble_shim.h').write_text('\n'.join(shim) + '\n')
    (tmp/'scrobble_test.c').write_text(SCROBBLE)
    binary = tmp/'scrobble_test'
    subprocess.run(['cc', '-pthread', '-DPEQ_HOST', f'-DPEQ_ROOT="{root}"', f'-DROOT="{root}"', '-D_GNU_SOURCE',
                    '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter', '-I', str(ROOT/'patch'), '-include', str(tmp/'scrobble_shim.h'),
                    str(ROOT/'patch/scrobble.c'), str(tmp/'scrobble_test.c'), '-lcrypto', '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
    requests = [line.split('\t', 3) for line in (root/'requests').read_text().splitlines()]
    artist = lambda i: f'Art "{i}" \\ &=+%é'
    lb_sizes, fm_sizes = [], []
    for url, headers, verify, body in requests:
        if 'listenbrainz' in url:
            assert url == 'https://api.listenbrainz.org/1/submit-listens'
            assert headers == 'Authorization: Token tok|Content-Type: application/json|'
            listens = json.loads(body)['payload']
            assert json.loads(body)['listen_type'] == 'import'
            for x in listens:
                i = x['listened_at'] - 1700000000; m = x['track_metadata']
                assert m['artist_name'] == artist(i) and m['track_name'] == f'Title {i}'
                assert m.get('release_name') == ('Alb/um' if i % 10 else None)
            lb_sizes.append(len(listens))
            continue
        assert url == 'https://ws.audioscrobbler.com/2.0/' and not headers
        params = urllib.parse.parse_qsl(body, keep_blank_values=True, strict_parsing=True)
        assert params[-2:][1] == ('format', 'json') and params[-2][0] == 'api_sig'
        signed = sorted(params[:-2])
        assert [k for k, _ in params[:-2]] == [k for k, _ in signed]  # sent in signature order
        assert params[-2][1] == hashlib.md5((''.join(k + v for k, v in signed) + 'sec').encode()).hexdigest()
        p = dict(params)
        if p['method'] == 'auth.getMobileSession':
            assert (p['username'], p['password'], p['api_key']) == ('u', 'p&w', 'key'); continue
        assert p['method'] == 'track.scrobble' and p['sk'] == 'SK123'
        n = sum(k.startswith('artist[') for k in p)
        for j in range(n):
            i = int(p[f'timestamp[{j}]']) - 1700000000
            assert p[f'artist[{j}]'] == artist(i) and p[f'track[{j}]'] == f'Title {i}' and p[f'duration[{j}]'] == str(200 + i)
            assert p.get(f'album[{j}]') == ('Alb/um' if i % 10 else None)
        fm_sizes.append(n)
    assert lb_sizes == [50, 50, 20, 50, 10, 10, 1, 1] and fm_sizes == [50, 50, 20, 50]
    assert [v for _, _, v, _ in requests] == ['1'] * len(requests)
    print('Scrobble upload: config, batching, JSON and form escaping, Last.fm signatures, log rewrite and failures passed.')

def books_check(tmp):
    """Books (books.c): raw inflate against zlib and on garbage, XHTML to text, EPUB to text (stored
    and deflated entries, namespaced OPF, %XX hrefs, DRM, malformed zips), UTF-8 with the Latin-1
    fallback, and page layout forward and back."""
    import random, zipfile, zlib
    lib = compile_host(tmp, 'books.so', ROOT/'patch/books.c')
    lib.book_inflate.argtypes = [C.c_char_p, C.c_uint, C.c_char_p, C.c_uint]
    rng = random.Random(7)
    words = [bytes(rng.choice(b'abcdefghij ') for _ in range(rng.randrange(1, 9))) for _ in range(200)]
    samples = [b'', b'a', bytes(range(256)) * 40, b' '.join(rng.choice(words) for _ in range(30000)),
               bytes(rng.randrange(256) for _ in range(5000))]
    for data in samples:
        for level, strategy in ((0, 0), (1, 0), (6, 0), (9, 0), (6, zlib.Z_FIXED), (6, zlib.Z_HUFFMAN_ONLY), (6, zlib.Z_RLE)):
            c = zlib.compressobj(level, zlib.DEFLATED, -15, 9, strategy); packed = c.compress(data) + c.flush()
            out = C.create_string_buffer(len(data) + 1)
            assert lib.book_inflate(packed, len(packed), out, len(data)) == len(data) and out.raw[:len(data)] == data
            if data: assert lib.book_inflate(packed, len(packed), out, len(data) - 1) == -1  # longer than its room
            if data: assert lib.book_inflate(packed[:len(packed) // 2], len(packed) // 2, out, len(data)) == -1
    out = C.create_string_buffer(1 << 16)
    for _ in range(3000):  # garbage never reads or writes out of bounds, and ends
        junk = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 64)))
        assert -1 <= lib.book_inflate(junk, len(junk), out, 1 << 16) <= 1 << 16
    xhtml = lib.book_xhtml; xhtml.argtypes = [C.c_char_p]
    def text(s):
        b = C.create_string_buffer(s.encode()); n = xhtml(b); return b.raw[:n].decode()
    assert text('<?xml version="1.0"?><!DOCTYPE html><html><head><title>T</title><style>p{}</style></head>'
                '<body><h1>One</h1>\n  <p>A  <i>b</i>\tc&amp;d &lt;&#233;&#x1F600;&gt; &nbsp;e&bogus; &#xZZ;</p>'
                '<!-- <p>hidden</p> --><script>x<y</script><p>f<br/>g</p><div><p>h</p></div>&quot;&apos;</body></html>') == \
        'One\n\nA b c&d <\u00e9\U0001F600>  e&bogus; &#xZZ;\n\nf\ng\n\nh\n\n"\''
    assert text('<p>no end') == 'no end' and text('<head>never closed') == '' and text('&#1114112;&#0;<') == '&#1114112;&#0;'
    def epub(path, files, method=zipfile.ZIP_DEFLATED, container=True):
        with zipfile.ZipFile(path, 'w', method) as z:
            z.writestr(zipfile.ZipInfo('mimetype'), 'application/epub+zip')
            if container:
                z.writestr('META-INF/container.xml', '<?xml version="1.0"?><container><rootfiles>'
                           '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
            for n, d in files.items(): z.writestr(n, d)
    opf = ('<opf:package><opf:manifest><opf:item id="c2" href="Text/two%20b.xhtml#top" media-type="application/xhtml+xml"/>'
           "<opf:item href='Text/one.xhtml' id='c1'/><opf:item id=\"img\" href=\"cover.jpg\" media-type=\"image/jpeg\"/><opf:item id=\"empty\" href=\"e.xhtml\"/>"
           '</opf:manifest><opf:spine><opf:itemref idref="img"/><opf:itemref idref="c1"/><opf:itemref idref="empty"/>'
           '<opf:itemref idref="missing"/><opf:itemref idref="c2"/></opf:spine></opf:package>')
    files = {'OEBPS/content.opf': opf, 'OEBPS/Text/one.xhtml': '<html><body><p>Hello caf\u00e9</p></body></html>' * 50,
             'OEBPS/Text/two b.xhtml': '<p>Second</p>', 'OEBPS/e.xhtml': '<html><head><title>x</title></head></html>',
             'OEBPS/cover.jpg': b'\xff\xd8\xff\xe0'}
    convert = lib.book_convert; convert.argtypes = [C.c_char_p, C.c_char_p, C.POINTER(C.c_int)]
    cancel = C.c_int(0); dst = tmp/'book.txt'
    def run(src): return convert(str(src).encode(), str(dst).encode(), C.byref(cancel))
    want = '\n\n'.join(['Hello caf\u00e9'] * 50) + '\f\nSecond'
    for method in (zipfile.ZIP_DEFLATED, zipfile.ZIP_STORED):
        epub(tmp/'a.epub', files, method); assert run(tmp/'a.epub') == 1 and dst.read_text() == want
    # ../ and ./ in an href resolve against the OPF's folder
    epub(tmp/'r.epub', {**files, 'OEBPS/content.opf': opf.replace("'Text/one.xhtml'", "'../OEBPS/./Text/one.xhtml'")})
    assert run(tmp/'r.epub') == 1 and dst.read_text() == want
    epub(tmp/'f.epub', {**files, 'META-INF/encryption.xml': '<CipherReference URI="OEBPS/Fonts/a.otf"/>'})
    assert run(tmp/'f.epub') == 1  # obfuscated fonts alone
    epub(tmp/'d.epub', {**files, 'META-INF/encryption.xml': '<CipherReference URI="OEBPS/Text/one.xhtml"/>'})
    assert run(tmp/'d.epub') == 0
    epub(tmp/'n.epub', files, container=False); assert run(tmp/'n.epub') == 0
    whole = (tmp/'a.epub').read_bytes()
    for cut in (0, 10, len(whole) // 2, len(whole) - 30):
        (tmp/'t.epub').write_bytes(whole[:cut]); assert run(tmp/'t.epub') == 0
    for _ in range(200):  # flipped bytes: a refusal or some text, never a crash
        b = bytearray(whole); b[rng.randrange(len(b))] ^= 1 << rng.randrange(8); (tmp/'x.epub').write_bytes(b); run(tmp/'x.epub')
    assert run(tmp/'none.epub') == 0
    cancel.value = 1; assert run(tmp/'a.epub') == 0; cancel.value = 0
    lib.book_char.argtypes = [C.c_char_p, C.c_uint, C.POINTER(C.c_uint)]
    def chars(b):
        i = C.c_uint(0); out = []
        while i.value < len(b): out.append(lib.book_char(b, len(b), C.byref(i)))
        return out
    assert chars('a\u00e9\u20ac\U0001F600'.encode()) == [97, 0xe9, 0x20ac, 0x1f600]
    assert chars(b'\xe9t\xc3') == [0xe9, ord('t'), 0xc3] and chars(b'\xe2\x82') == [0xe2, 0x82]
    MEASURE = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_uint); LINE = C.CFUNCTYPE(None, C.c_void_p, C.c_int, C.POINTER(C.c_uint), C.c_int)
    lines = []
    measure = MEASURE(lambda ctx, c: 10); line = LINE(lambda ctx, row, s, n: lines.append((row, ''.join(map(chr, s[:n])))))
    lib.book_page.argtypes = [C.c_char_p, C.c_uint, C.c_uint, C.c_int, C.c_int, MEASURE, LINE, C.c_void_p]
    lib.book_back.argtypes = [C.c_char_p, C.c_uint, C.c_uint, C.c_int, C.c_int, MEASURE, C.c_void_p]
    def page(t, pos, rows=3, width=100):
        lines.clear(); return lib.book_page(t, len(t), pos, rows, width, measure, line, None), [l for _, l in lines]
    t = b'\n\xef\xbb\xbfThe quick brown fox\r\njumps over the lazy dog\n\nAndsuperlongwordhere end\fNext'
    # Wraps at the last space (a space at the edge is used up), mid-word without one; the BOM and
    # the top's line breaks are dropped; a \f ends the page; a page from it starts after it.
    assert page(t, 0, rows=9) == (t.index(b'\f'), ['The quick', 'brown fox', 'jumps over', 'the lazy', 'dog', '', 'Andsuperlo', 'ngwordhere', 'end'])
    assert page(t, t.index(b'\f'), rows=9) == (len(t), ['Next'])
    assert page(t, 0) == (t.index(b'the'), ['The quick', 'brown fox', 'jumps over'])
    # Back: in a text shorter than the look-back, exactly the page before; beyond it, a page that
    # reaches the one asked about.
    starts = [0]
    while starts[-1] < len(t): starts.append(page(t, starts[-1])[0])
    for a, b in zip(starts, starts[1:-1]): assert lib.book_back(t, len(t), b, 3, 100, measure, None) == a
    assert lib.book_back(t, len(t), 0, 3, 100, measure, None) == 0
    big = b'\n'.join(b' '.join(rng.choice(words) for _ in range(rng.randrange(1, 40))) for _ in range(800))
    pos = 0
    for _ in range(60):
        pos = page(big, pos, rows=10, width=320)[0]
        b = lib.book_back(big, len(big), pos, 10, 320, measure, None)
        assert b < pos and page(big, b, rows=10, width=320)[0] >= pos and pos - b < 3072
    print('Books: inflate, XHTML text, EPUB conversion and refusals, UTF-8 and page layout passed.')

def video_check(tmp):
    """Videos' player (video.c): ffmpeg's argv for the Q2's framebuffer, frame pacing and BT volume."""
    lib = compile_host(tmp, 'q2video.so', ROOT/'patch/video.c')
    def argv(at, audio):
        a, ss = (C.c_char_p * 27)(), C.create_string_buffer(16)
        lib.ffmpeg_argv(a, ss, at, b'/mnt/mmc/Videos/a b.mp4', audio)
        return [x.decode() for x in a[:a[:].index(None)]]
    # /dev/fb0 is 320x375 BGRA (display_logo, soc_fb.ko): fitted into the landscape 375x320 view and
    # turned clockwise, as the boot logo.
    head = ['/usr/bin/ffmpeg', '-nostdin', '-loglevel', 'quiet', '-ss', '30', '-i', '/mnt/mmc/Videos/a b.mp4',
            '-map', '0:v:0', '-vf', 'scale=375:320:force_original_aspect_ratio=decrease:flags=fast_bilinear,format=bgra,'
            'pad=375:320:(ow-iw)/2:(oh-ih)/2,transpose=clock', '-r', '25', '-f', 'rawvideo', 'pipe:3']
    assert argv(30, 1) == head + ['-map', '0:a:0', '-ac', '2', '-ar', '48000', '-f', 's16le', 'pipe:4']
    assert argv(30, 0) == head
    # Frame n shows from n/25 s of the clock, and is dropped a whole frame late.
    lib.frame_due.argtypes = [C.c_int, C.c_longlong]
    assert [lib.frame_due(1, t) for t in (0, 39, 40, 79, 80)] == [0, 0, 1, 1, 2] and lib.frame_due(0, 0) == 1
    # Bluetooth: hciplayer's soft volume curve in 1/65536, clamped to 0-100, and the samples scaled by it.
    assert [lib.bt_gain(v) for v in (-5, 0, 1, 25, 49, 50, 75, 99, 100, 120)] == \
        [0, 0, 131, 3276, 6422, 6553, 36044, 64356, 65536, 65536]
    pcm = (C.c_short * 6)(32767, -32768, 1000, -1000, 1, -1)
    lib.scale(pcm, 6, 65536); assert list(pcm) == [32767, -32768, 1000, -1000, 1, -1]
    lib.scale(pcm, 5, lib.bt_gain(50)); assert list(pcm) == [3276, -3277, 99, -100, 0, -1]
    lib.scale(pcm, 6, 0); assert list(pcm) == [0] * 6
    # The length from ffmpeg -i's report, for the position bar.
    assert lib.duration(b'Input #0, mov\n  Duration: 01:02:03.45, start: 0.0\n') == 3723
    assert lib.duration(b'  Duration: N/A, bitrate: N/A') == 0 and lib.duration(b'') == 0
    # A quit's place, kept for next time, but 0 within 30 s of either end; an unknown length keeps it.
    assert [lib.resume_at(p, 600) for p in (0, 29, 30, 300, 570, 571, 600)] == [0, 0, 30, 300, 570, 0, 0]
    assert lib.resume_at(5000, 0) == 5000
    for paused in (0, 1):
        assert lib.video_pause(paused, ord('a')) == 1
        assert lib.video_pause(paused, ord('r')) == 0
        assert lib.video_pause(paused, ord('p')) == 1-paused
        assert lib.video_pause(paused, ord('f')) == paused
    # The bar: along the bottom of the picture as seen, 40 px in from its ends and 23-30 px up, white
    # for the share given and black after; the picture is turned clockwise, so the bar runs down
    # columns 22-29 (picture x = panel y), from the top. Nothing else changes.
    def bar(n, total, line=1300):  # a padded line, as fix.line_length may be
        f = (C.c_ubyte * (line * 375))(*([0x55] * (line * 375)))
        lib.overlay(f, line, n, total)
        return [[f[y * line + x * 4 + b] for x in range(320) for b in range(4)] for y in range(375)], f
    p, f = bar(1, 4)
    for y in range(375):
        for x in range(320 * 4):
            want = 0x55 if not (22 * 4 <= x < 30 * 4 and 40 <= y < 335) else 0xff if y < 40 + 295 // 4 else 0
            assert p[y][x] == want, (x, y)
    assert all(f[y * 1300 + c] == 0x55 for y in range(375) for c in range(1280, 1300))
    assert [r[88] for r in bar(7, 5)[0][40:335]] == [0xff] * 295  # clamped to all
    assert [r[88] for r in bar(-3, 5)[0][40:335]] == [0] * 295 and bar(1, 0)[0][100][88] == 0x55
    # Internet Radio (-r): the ICY tags in stock ffmpeg 4.2's report, in its input dump or as updates.
    out = C.create_string_buffer(16)
    def meta(line, key, n=16):
        return lib.meta(line, key, out, n) and out.value.decode()
    assert meta(b'    icy-br          : 128\n', b'icy-br') == '128' and meta(b'    icy-name   : X', b'icy-br') == 0
    assert meta(b'[http @ 0x7f] Metadata update for StreamTitle: A - B\r', b'StreamTitle') == 'A - B'
    assert meta(b'    StreamTitle     : ', b'StreamTitle') == '' and meta(b'StreamTitle=x', b'StreamTitle') == 0
    assert meta(b'  Stream #0:0: Audio: mp3 (mp3float), 44100 Hz', b'Audio') == 'mp3 (mp3float),'
    assert meta(b'StreamTitle: a long title past the buffer', b'StreamTitle', 8) == 'a long '
    print('Videos: ffmpeg argv, frame pacing, Bluetooth volume, length and bar passed; radio tags passed.')


def audio_writer_check(tmp):
    """Exercise the actual shared writer with partial writes, underruns and lost outputs."""
    source = tmp/'writer_test.c'
    source.write_text(r'''#define main video_main
#define read mock_read
#define snd_pcm_writei mock_write
#define snd_pcm_recover mock_recover
#define snd_pcm_delay mock_delay
#define usleep mock_sleep
#include "video.c"
#undef main
extern void abort(void);
#define CHECK(x) do { if (!(x)) abort(); } while (0)
static int reads, calls, recoveries, sleeps, mode;
static unsigned tail_out;
static const short *base;
int mock_read(int fd, void *b, unsigned n) {
    if (mode==3) return reads++ ? (reads==2 ? 1024 : 0) : 2048; /* 768 frames, then EOF */
    CHECK(n==2048);
    if (reads++) return 0;
    for (unsigned i=0; i<n/2; ++i) ((short *)b)[i]=(short)i;
    return (int)n;
}
long mock_write(void *pcm, const void *b, unsigned long n) {
    if (mode==3) return tail_out += n, (long)n;
    if (!calls) base=b;
    ++calls;
    if (mode==1) return -32;
    if (mode==2) return -19;
    if (calls==1) { CHECK(n==512); return 128; }
    CHECK(b==base+256 && n==384 && *(const short *)b==256);
    if (calls==2) return -32;
    if (calls==3) return 0;
    return 384;
}
int mock_recover(void *pcm, int err, int silent) {
    ++recoveries;
    return err==-19 ? err : 0;
}
int mock_delay(void *pcm, long *delay) { *delay=mode==3 ? 100 : 0; return 0; }
const char *snd_strerror(int err) { return "mock error"; }
int mock_sleep(unsigned n) { CHECK(n==1000); ++sleeps; return 0; }
int main(void) {
    au.gain=65536;
    writer(0);
    CHECK(au.done && au.played==512 && calls==4 && recoveries==1 && sleeps==1);
    for (mode=1; mode<=2; ++mode) {
        reads=calls=recoveries=0; au.done=0;
        writer(0);
        CHECK(au.done && recoveries==1 && calls==(mode==1 ? 2 : 1));
        CHECK(au.error[0]);
    }
    reads=0; au.done=0; mode=3;
    writer(0);
    CHECK(au.done && tail_out==768 && au.played==668); /* still behind the buffer at the end */
    return 0;
}
''')
    binary=tmp/'writer_test'
    subprocess.run(['cc','-m32','-fno-builtin','-ffunction-sections','-fdata-sections',
                    '-I',str(ROOT/'patch'),str(source),'-Wl,--gc-sections','-o',str(binary)],check=True)
    subprocess.run([str(binary)],check=True)
    print('Audio writer: partial frames retained, underrun retry, zero progress, output failures and end delay passed.')

def radio_check(tmp):
    """Internet Radio (radio.c): .m3u and .pls favourites, radio-browser's m3u and CSV, URL escaping."""
    lib = compile_host(tmp, 'radio.so', ROOT/'patch/radio.c')
    def parse(fn, text, *extra):
        buf, a, b = C.create_string_buffer(text), (C.c_uint * 8)(), (C.c_uint * 8)()
        n = fn(buf, a, b, *extra)
        return [(C.string_at(C.addressof(buf) + a[i]).decode(), C.string_at(C.addressof(buf) + b[i]).decode()) for i in range(n)]
    playlist = lambda text, max=8: parse(lib.playlist, text, max)
    # #EXTINF names the next URL (CRLF, a BOM, radio-browser's extra tags); a bare URL names itself.
    assert playlist(b'\xef\xbb\xbf#EXTM3U\r\n#RADIOBROWSERUUID:1\r\n#EXTINF:-1,A, B\r\nhttp://a/1?x=1\r\n\r\n'
                    b'  https://b/2\n#EXTINF:1,\nhttp://c/3\n/local/file.mp3\n') == \
        [('A, B', 'http://a/1?x=1'), ('https://b/2', 'https://b/2'), ('http://c/3', 'http://c/3')]
    # .pls: FileN is a URL, TitleN names it; other keys are skipped. At most max.
    assert playlist(b'[playlist]\nFile1=http://p/1\nTitle1=One\nLength1=-1\nfile2=http://p/2\nNumberOfEntries=2\n') == \
        [('One', 'http://p/1'), ('http://p/2', 'http://p/2')]
    assert playlist(b'http://a\nhttp://b\nhttp://c\n', 2) == [('http://a',) * 2, ('http://b',) * 2] and playlist(b'') == []
    assert playlist(b'[playlist]\nTitle2=Two\nFile1=http://p/1\nFile2=http://p/2\nTitle1=One\n', 2) == \
        [('One', 'http://p/1'), ('Two', 'http://p/2')]
    assert playlist(b'File1=http://p/1\nTitle1=One\nFile2=http://p/2\nTitle2=Two\n', 1) == \
        [('One', 'http://p/1')]
    # The CSV after its header: a country's code, or the tag itself; a quoted name keeps its commas.
    countries = b'name,iso_3166_1,stationcount\n"Taiwan, Republic Of China",TW,214\r\nGermany,DE,6496\n,XX,1\nbad\n'
    assert parse(lib.choices, countries, 8, 1) == [('Taiwan, Republic Of China', 'TW'), ('Germany', 'DE')]
    assert parse(lib.choices, b'name,stationcount\npop,6387\nclassic rock,3312\n', 8, 0) == \
        [('pop', 'pop'), ('classic rock', 'classic rock')]
    out = C.create_string_buffer(64)
    lib.url_escape('drum & bass/é~'.encode(), out, 64); assert out.value == b'drum%20%26%20bass%2F%C3%A9~'
    lib.url_escape(b'abc def', out, 6); assert out.value == b'abc'  # never a cut escape
    print('Internet Radio: m3u and pls favourites, the directory\'s m3u and CSV, URL escaping passed.')

def spotify_check(tmp):
    """Spotify's browser (spotify.c): Q2SPOT/1 replies cut in place and fields escaped."""
    lib = compile_host(tmp, 'spotify.so', ROOT/'patch/spotify.c')
    def reply(text, max=50):
        buf, f = C.create_string_buffer(text), (C.c_char_p * (4 + 6 * max))()
        n = lib.spot_reply(buf, len(text), f, max)
        return n if n < 0 else (f[:4], [f[4 + 6 * i:10 + 6 * i] for i in range(n)])
    # The header, then rows; %25, %09, %0A and %0D (any case) are the bytes, UTF-8 literal.
    assert reply(b'Q2SPOT/1\n7\tok\t\tCg4%25x\ntrack\tspotify:track:1\tA%09B%0a%0D\t\xc3\xa9\tspotify:user:a:collection\t0\n') == \
        ([b'7', b'ok', b'', b'Cg4%x'], [[b'track', b'spotify:track:1', b'A\tB\n\r', b'\xc3\xa9', b'spotify:user:a:collection', b'0']])
    assert reply(b'Q2SPOT/1\n3\tlogin\tOpen Spotify\t') == ([b'3', b'login', b'Open Spotify', b''], [])
    # Malformed: no version, a bad or cut escape, %00, a NUL, a line of the wrong width, too many rows.
    for bad in (b'Q2SPOT/2\n1\tok\t\t\n', b'', b'Q2SPOT/1\n', b'Q2SPOT/1\n1\tok\t%zz\t\n', b'Q2SPOT/1\n1\tok\t\t%2',
                b'Q2SPOT/1\n1\tok\t%00\t\n', b'Q2SPOT/1\n1\tok\t\0\t\n', b'Q2SPOT/1\n1\tok\t\n',
                b'Q2SPOT/1\n1\tok\t\t\t\n', b'Q2SPOT/1\n1\tok\t\t\ntrack\tu\tt\ts\tc\n'):
        assert reply(bad) == -1, bad
    two = b'Q2SPOT/1\n1\tok\t\t\n' + b'album\tu\tt\ts\t\t\n' * 2
    assert len(reply(two, 2)[1]) == 2 and reply(two, 1) == -1
    out = C.create_string_buffer(20)
    assert lib.spot_escape(b'a%b\tc\nd\re\xc3\xa9', out, 20) == 19 and out.value == b'a%25b%09c%0Ad%0De\xc3\xa9'
    assert lib.spot_escape(b'abc', out, 3) == -1 and lib.spot_escape(b'%', out, 3) == -1 and lib.spot_escape(b'%', out, 4) == 3
    print('Spotify: Q2SPOT/1 replies, escapes and malformed input passed.')

TIDAL_DL = r'''#include <assert.h>
#include <dirent.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#define tk_snprintf snprintf
#define pthread_mutex_lock(m) pthread_mutex_lock((pthread_mutex_t *)(m))
#define pthread_mutex_unlock(m) pthread_mutex_unlock((pthread_mutex_t *)(m))
#define I(p, o) (*(int *)((char *)(p) + (o)))
#define P(p, o) (*(void **)((char *)(p) + (o)))
#define REC_TIDAL_ID 0
#define REC_TITLE 0
#define REC_ARTIST 0
static int quality = 27; static void *queue;
#define tidalStreamingLevelNow (&quality)
#define mcl_pdeqplaylist (&queue)
static _Atomic int status = 1, gate, performing, performs, ended;
static _Atomic unsigned long long clk = 1000;
static char last[64];
static int mclGetPlayStatus(void) { return status; }
static unsigned long long now_ms(void) { return clk; }
static int deque_size(void *q) { return 0; }
static void *deque_at(void *q, unsigned i) { return 0; }
static void headset_manual(void) {}
static int card_space(const char *d) { return 1; }
static int stream_cache(int s) { return 2; }
int stock_tidal_trampoline(const char *u) { return 0; }
static int __xstat(int v, const char *p, unsigned *st) {
    struct stat s; if (stat(p, &s)) return -1; st[12] = s.st_size, st[16] = s.st_mtime; return 0;
}
typedef unsigned (*sink_t)(const void *, unsigned, unsigned, void *);
typedef int (*prog_t)(void *, long long, long long, long long, long long);
static struct { sink_t sink; void *data; prog_t prog; void *pdata; const char *url; } h;
static void *curl_easy_init(void) { return &h; }
static int curl_easy_setopt(void *c, int o, ...) {
    va_list ap; va_start(ap, o);
    if (o == 10002) h.url = va_arg(ap, const char *);
    if (o == 20011) h.sink = va_arg(ap, sink_t);
    if (o == 10001) h.data = va_arg(ap, void *);
    if (o == 20219) h.prog = va_arg(ap, prog_t);
    if (o == 10057) h.pdata = va_arg(ap, void *);
    va_end(ap); return 0;
}
static int curl_easy_perform(void *c) {
    snprintf(last, sizeof last, "%s", h.url); ++performs; performing = 1;
    while (gate) if (h.prog(h.pdata, 0, 0, 0, 0)) { performing = 0; return 42; } else usleep(1000);
    h.sink("data", 1, 4, h.data); performing = 0; return 0;
}
static int curl_easy_getinfo(void *c, int i, long *code) { *code = 200; return 0; }
static void curl_easy_cleanup(void *c) { }
static int renamed(const char *a, const char *b) { int r = rename(a, b); ++ended; return r; }
static int unlinked(const char *p) { int r = unlink(p); ++ended; return r; }
#define rename renamed
#define unlink unlinked
TIDAL_SOURCE
static void wait_for(_Atomic int *v, int want) { for (int i = 0; i < 3000 && *v != want; i++) usleep(1000); assert(*v == want); }
static int has(const char *id) { char p[300]; snprintf(p, sizeof p, TIDAL_DIR "/%s-27.flac", id); return !access(p, 0); }
static void play(const char *id) { char u[64]; snprintf(u, sizeof u, "https://x/%s.flac", id); tidal_play(u, id); }
static void finish(int n) { usleep(20000); gate = 0; wait_for(&ended, n); usleep(20000); }
int main(void) {
    gate = 1; play("A"); wait_for(&performing, 1); /* stopped 2 s between tracks, then playing: kept */
    clk += 2000; usleep(20000); status = 2; finish(1); assert(has("A"));
    gate = 1; play("B"); wait_for(&performing, 1); /* stopped 3 s: Stop, dropped */
    status = 1; usleep(20000); clk += 3001; wait_for(&ended, 2); assert(!has("B"));
    status = 2; gate = 1; play("C"); wait_for(&performing, 1); /* the same id again: one transfer */
    play("C"); finish(3); assert(has("C") && performs == 3);
    gate = 1; play("D"); wait_for(&performing, 1); /* a skip: D dropped, the same worker takes E */
    play("E"); wait_for(&ended, 4); wait_for(&performing, 1); assert(!strcmp(last, "https://x/E.flac"));
    finish(5); assert(!has("D") && has("E"));
    gate = 1; play("F"); wait_for(&performing, 1); /* G's job overtaken by a cached play: dropped */
    play("G"); play("A"); finish(6); usleep(50000); assert(!has("F") && !has("G") && performs == 6);
    return 0;
}
'''

def tidal_check(tmp):
    """Tidal cache (tidal.c): the cached file's extension comes from the stream URL's path."""
    lib = compile_host(tmp, 'tidal.so', ROOT/'patch/tidal.c')
    lib.tidal_ext.restype = C.c_char_p
    ext = lambda url, n=8: lib.tidal_ext(url, C.create_string_buffer(n), n)
    assert ext(b'https://sp-pr-fa.audio.tidal.com/mediatracks/AbC/0.flac?token=1.2') == b'.flac'
    assert ext(b'https://x/a/b.mp4') == b'.mp4'
    assert ext(b'https://x.y/a/b?f=c.flac') is None and ext(b'https://x/a.') is None  # no extension in the path
    assert ext(b'https://x/a.longextension') is None and ext(b'https://x/a.flac', 5) is None  # too long for out
    lib.tidal_name.restype = C.c_char_p
    name = lambda n, artist, title, ext=b'.flac': lib.tidal_name(C.create_string_buffer(n), n, b'42-3', artist, title, ext)
    assert name(64, b'A/B', b'Song: "1"?') == b'42-3 A_B - Song_ _1__.flac'
    assert name(64, None, b'Solo') == b'42-3 Solo.flac' and name(64, b'A', b'') == b'42-3.flac'
    assert name(16, b'Artist', b'Title') == b'42-3 Artis.flac'  # cut to fit
    assert name(16, b'', '\u00e9\u00e9\u00e9'.encode()) == '42-3 \u00e9\u00e9.flac'.encode()  # never mid-character
    assert name(9, b'A', b'T') is None
    # Run the actual eviction loop with a synthetic directory, including a failed unlink.
    trim = (ROOT/'patch/tidal.c').read_text().split('static void trim(void) {', 1)[1].split('static unsigned save', 1)[0]
    source = tmp/'tidal_trim.c'
    source.write_text(r'''#include <assert.h>
#include <stdio.h>
#include <string.h>
#define TIDAL_DIR "/cache"
#define tk_snprintf snprintf
struct dirent { char d_name[16]; };
static int fail, left, cursor, attempts, choice = 2;
static int stream_cache(int spotify) { assert(!spotify); return choice; } /* 512 << choice MB */
static void *opendir(const char *p) { cursor=0; return (void *)1; }
static struct dirent *readdir(void *d) {
    static struct dirent e;
    if (cursor == left) return 0;
    snprintf(e.d_name, sizeof e.d_name, "%d.flac", cursor++); return &e;
}
static int closedir(void *d) { return 0; }
static int __xstat(int v, const char *p, unsigned *s) {
    s[12]=1200u<<20; s[16]=1; return 0;
}
static int unlink(const char *p) { ++attempts; if (fail) return -1; --left; return 0; }
static void trim(void) {''' + trim + r'''
int main(void) {
    left=3; trim(); assert(left==1 && attempts==2);
    fail=1; left=3; attempts=0; trim(); assert(left==3 && attempts==1);
    fail=0; choice=3; attempts=0; trim(); assert(left==3 && !attempts); /* 4 GB holds 3600 MB */
    choice=0; trim(); assert(left==0 && attempts==3); /* 512 MB holds none of 1200 */
    return 0;
}
''')
    binary = tmp/'tidal_trim'
    subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter', str(source), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=5)
    import re
    # The download worker on a real pthread, curl scripted: perform runs the progress callback while
    # gate is open, then hands over a body (unless cancelled) and the worker renames or unlinks.
    body = re.sub(r'^(#include|#ifndef PEQ_HOST|#endif|__asm__).*$', '', (ROOT/'patch/tidal.c').read_text(), flags=re.M)
    (tmp/'tidal_dl.c').write_text(TIDAL_DL.replace('TIDAL_SOURCE', body))
    binary = tmp/'tidal_dl'
    subprocess.run(['cc', '-pthread', '-D_GNU_SOURCE', f'-DTIDAL_DIR="{tmp}/tidal"', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                    str(tmp/'tidal_dl.c'), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=30)
    print('Tidal cache: stream file extensions, eviction at the chosen cap, failed deletion, Stop, re-fetch and skip downloads passed.')

if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--captures', type=pathlib.Path, help="also draw 3EQ's page in both iPod themes as PNGs here")
    ap.add_argument('--font', type=pathlib.Path, help='the stock font for the captures (a build directory\'s default.ttf)')
    args = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix='q2-peq-check-') as directory:
        tmp = pathlib.Path(directory); lib = library(tmp)
        parser_check(lib, tmp)
        dsp_check(lib)
        eq3_check(lib, tmp)
        editor_check(lib, tmp)
        player_check(tmp)
        visualizer_check(tmp)
        scrobble_check(tmp)
        books_check(tmp)
        video_check(tmp)
        audio_writer_check(tmp)
        radio_check(tmp)
        spotify_check(tmp)
        tidal_check(tmp)
        if args.captures:
            args.captures.mkdir(parents=True, exist_ok=True)
            (tmp/'captures').mkdir()
            eq3_captures(tmp/'captures', args.captures, args.font or 'DejaVu-Sans')
