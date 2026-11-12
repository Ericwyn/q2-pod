#!/usr/bin/env python3
"""Focused Chinese-build MIPS regressions. Run: python test/chinese.py BUILD_DIRECTORY.

The real patched entry points run under Unicorn; network calls are mocked, never sent.
The shared fixture validates the manifest/source and stock/payload hashes first.
"""
import pathlib

source = pathlib.Path(__file__).with_name('patch.py').read_text()
exec(compile(source[:source.index('checks=0')], 'test/patch.py', 'exec'))

# Reuse the full suite's actual stock-poll and semaphore ABI assertions, independently of UI wording.
start = source.index('# Stream threads (tidal.c):')
end = source.index('# EQ per Output (headset.c', start)
checks = 0

def passed():
    global checks
    checks += 1

exec(compile(source[start:end], 'test/patch.py', 'exec'))
assert HOOKS['kugou_obtainlyric'][2] == 'kugoulyric'


class Lyrics(Machine):
    def __init__(self, answers, native=2):
        self.answers, self.native, self.asked, self.fallback = list(answers), native, [], []
        super().__init__()
        self.handlers[syms['online_getkeyword_info']] = 'cn:key'
        self.handlers[payload_syms['lrclib']] = 'cn:lrclib'
        self.handlers[payload_syms['stock_kugoulyric_trampoline']] = 'cn:kugou'

    def hook(self, u, address, size, unused):
        name = self.handlers.get(address)
        a, b, c = [u.reg_read(r) for r in REGS[:3]]
        if name == 'cn:key':
            u.mem_write(a, bytes(0x600))
            u.mem_write(a + 0x200, b'T\0')
            u.mem_write(a + 0x500, b'A\0')
            return self.returns(0, clobber=True)
        if name == 'cn:lrclib':
            self.asked.append((self.text(a), self.text(b), self.text(c)))
            return self.returns(self.answers.pop(0), clobber=True)
        if name == 'cn:kugou':
            self.fallback.append((self.text(a), self.text(b)))
            return self.returns(self.native, clobber=True)
        return super().hook(u, address, size, unused)


# Saved, no match, network failure, write failure, busy exhaustion, busy then success.
for answers, native, want in [([1], 2, 1), ([0], 2, 2), ([-2], 0, 0), ([-3], -1, -1),
                               ([-1, -1, -1], 2, 2), ([-1, 1], 2, 1)]:
    m = Lyrics(answers, native)
    keyword, out = m.string('kw'), m.string('/tmp/oslyric_cn')
    assert m.call(address=HOOKS['kugou_obtainlyric'][0], args=(keyword, out, 0, 0), gap=0) == want
    assert not m.answers and m.asked == [('T', 'A', '/tmp/oslyric_cn')] * len(answers)
    assert m.fallback == ([] if answers[-1] == 1 else [('kw', '/tmp/oslyric_cn')])
    assert [call[1] for call in m.calls if call[0].startswith('usleep')] == [2000000] * (len(answers) - 1)
    passed()

# The native Classic caption and its shadow must both select a font containing Chinese.
for text, face, px in [('Finish', 'classic-light', 31), ('完成后按中心键', 'default', 26)]:
    m = Machine()
    m.config['THEME'] = '1'
    label = m.node('label', text=text)
    classic = m.string('classic-light')
    m.call(address=payload_syms['slider_label'], args=(label, 0, 228, 375), stack=(32, 31, classic), gap=0)
    assert m.nodes[label]['style:normal:font_name'] == face
    assert m.nodes[label]['style:normal:font_size'] == px
    wide = m.alloc(4 * (len(text) + 1))
    m.u.mem_write(wide, struct.pack('<%dI' % (len(text) + 1), *map(ord, text), 0))
    rect = m.alloc(16)
    m.u.mem_write(rect, struct.pack('<4i', 0, 229, 375, 32))
    m.call(address=payload_syms['text_shadow'], args=(m.canvas, m.string(face), wide, rect), stack=(px, 0x59), gap=0)
    assert [(t['text'], t['font']) for t in m.letters] == [(text, (face, px))]
    passed()

print(f'Chinese MIPS regressions: {checks} groups passed (Baidu poll, Tidal gate, domestic lyrics fallback, CJK fonts).')
