"""The canvas' UTF-8 boundary and the stock-language labels retained by the Chinese port."""
import ctypes as C
import subprocess

import pytest
import build


@pytest.fixture(scope='module')
def wide(tmp_path_factory):
    d = tmp_path_factory.mktemp('chinese-text')
    source = d/'wide.c'
    source.write_text('#include "localization.h"\n'
                      'unsigned widen(unsigned *out, unsigned n, const char *s) { return ui_wide(out, n, s); }\n')
    lib = d/'wide.so'
    subprocess.run(['cc', '-shared', '-fPIC', '-Wall', '-Wextra', '-Werror', '-I',
                    str(build.ROOT/'patch'), str(source), '-o', str(lib)], check=True)
    loaded = C.CDLL(str(lib))
    fn = loaded.widen
    fn.argtypes, fn.restype = [C.POINTER(C.c_uint), C.c_uint, C.c_char_p], C.c_uint

    def decode(text, cap=40):
        out = (C.c_uint * 40)(*[0xdeadbeef] * 40)
        n = fn(out, cap, text)
        assert all(c == 0xdeadbeef for c in out[n:])
        return ''.join(chr(c) for c in out[:n])
    return decode


@pytest.mark.parametrize('text', ['', 'Save', '保存', '上午', '下午', '中心键：再玩一次', 'Aé中文😀'])
def test_canvas_text_is_decoded_by_codepoint(wide, text):
    assert wide(text.encode()) == text


@pytest.mark.parametrize('raw,want', [(b'\xff', '\ufffd'), (b'\xc0\xaf', '\ufffd\ufffd'),
                                    (b'\xe4\xb8', '\ufffd\ufffd'), (b'\xed\xa0\x80', '\ufffd' * 3),
                                    (b'\xf4\x90\x80\x80', '\ufffd' * 4), (b'\xe4A', '\ufffdA')])
def test_invalid_utf8_is_replaced_without_losing_following_ascii(wide, raw, want):
    assert wide(raw) == want


@pytest.mark.parametrize('cap', [0, 1, 2, 5, 32, 40])
def test_canvas_limit_counts_characters_not_utf8_bytes(wide, cap):
    assert wide(('中文😀' * 20).encode(), cap) == ('中文😀' * 20)[:cap]


def test_stock_label_inputs_stay_pinned_and_replacements_are_chinese():
    labels = {key.decode(): (old.decode(), new.decode()) for key, old, new in build.LABELS}
    assert labels['player_playlist'] == ('Playlists', '播放队列')
    assert labels['playset_noplay'] == ('Headphones required for playback', '播放需连接耳机')
    assert labels['list_baidu'] == ('Baidu Netdisk(BETA)', '百度网盘')
