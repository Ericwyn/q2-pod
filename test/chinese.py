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

# Run config_init's real LANGUAGE read/store for missing and persisted values, including the
# English value left by upstream. This used to be asserted as English in the full suite.
from build import GP
start = source.index('# Every unit boots in Simplified Chinese')
end = source.index('# Home binds no key-up of its own', start)
exec(compile(source[start:end], 'test/patch.py', 'exec'))


def resource_table(locale):
    path = 'release/assets/default/raw/strings/' + locale + '.bin'
    data = subprocess.check_output(['unsquashfs', '-cat', str(B/'rootfs.squashfs'), path])
    # Check the actual installed resource, not only the loose build output.
    assert sha(data) == manifest['changed_assets'][path]['sha256']
    from build import STRING_LABELS, relabel
    original = subprocess.check_output(['unsquashfs', '-cat', str(B/'stock.squashfs'), path])
    assert data == relabel(original, STRING_LABELS[locale + '.bin'])
    n = 2 * struct.unpack_from('<H', data, 2)[0]
    texts = [data[o:data.index(b'\0', o)].decode() for o in struct.unpack_from(f'<{n}I', data, 4)]
    return dict(zip(texts[::2], texts[1::2]))


class LocalizedHome(Machine):
    """Real switch_language and Home visitor, with the AWTK locale/text services simulated."""
    def __init__(self):
        super().__init__()
        self.locale = None
        self.translated = []
        self.handlers[0x4f0cb4] = 'stock_locale'
        self.font_switch = self.get(GP - 0x4c28)
        self.handlers[self.font_switch] = 'stock_language_font'

    def hook(self, u, address, size, unused):
        if address == 0x4f0cb4:
            self.locale = self.text(u.reg_read(REGS[0]))
            self.returns(0)
            return
        if address == self.font_switch:
            self.returns(0)
            return
        if address == syms['widget_set_tr_text']:
            label, key = u.reg_read(REGS[0]), self.text(u.reg_read(REGS[1]))
            self.translated.append(key)
            self.nodes[label]['text'] = self.table[key]
            self.returns(0)
            return
        return super().hook(u, address, size, unused)


for held in ({}, {'LANGUAGE': '2'}, {'LANGUAGE': '6'}):
    m = LocalizedHome()
    m.mock('puts@GLIBC_2.0')
    m.config.update(held)
    for r, v in ((UC_MIPS_REG_SP, 0x7000f000), (UC_MIPS_REG_S0, m.alloc(0x100)),
                 (UC_MIPS_REG_S1, 0x780000), (UC_MIPS_REG_S4, 0x780000),
                 (UC_MIPS_REG_S2, m.string('0')), (UC_MIPS_REG_S6, 1)):
        m.u.reg_write(r, v)
    m.word(0x7000f000 + 0x18, GP)
    m.u.emu_start(0x5047dc, 0x504854, count=100000)
    language = m.u.mem_read(syms['g_language'], 1)[0]
    m.call(address=syms['switch_language'], args=(language, 0, 0, 0), gap=0)
    assert m.locale == 'zh_CN', (held, language, m.locale)
    m.table = resource_table(m.locale)
    for name, key, want in [('playing', 'small_playing', '正在播放'),
                            ('folder', 'small_folder', '文件夹'),
                            ('stream', 'small_stream', '流媒体')]:
        label = m.node('label', name='label_' + name)
        # The native visitor reads widget_t.name directly; the fixture normally keeps it in nodes.
        m.word(label + 0x10, m.string('label_' + name))
        m.call(address=0x534750, args=(0, label, 0, 0), gap=0)
        assert m.translated[-1] == key and m.nodes[label]['text'] == want
    assert m.table['local_songer'] == '表演者' and m.table['player_playlist'] == '播放队列'
    assert m.config == held  # no factory reset or config rewrite needed
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

print(f'Chinese MIPS regressions: {checks} groups passed (Baidu poll, Tidal gate, domestic lyrics fallback, Chinese boot/Home resources, CJK fonts).')
