#ifndef Q2_LOCALIZATION_H
#define Q2_LOCALIZATION_H

/* Widen display text without splitting a UTF-8 character at the canvas' codepoint limit.
 * Invalid, overlong, surrogate and out-of-range sequences consume one byte as U+FFFD. */
static inline unsigned ui_wide(unsigned *out, unsigned cap, const char *text) {
    const unsigned char *p = (const unsigned char *)text;
    unsigned n = 0;
    while (*p && n < cap) {
        unsigned c = *p, bytes = c < 0x80                 ? 1
                                 : c >= 0xc2 && c <= 0xdf ? 2
                                 : c >= 0xe0 && c <= 0xef ? 3
                                 : c >= 0xf0 && c <= 0xf4 ? 4
                                                          : 0;
        unsigned v = bytes == 1 ? c : bytes ? c & (0x7f >> bytes) : 0;
        unsigned k = 1;
        for (; k < bytes; ++k) {
            if ((p[k] & 0xc0) != 0x80) break;
            v = v << 6 | (p[k] & 63);
        }
        if (!bytes || k != bytes || (bytes == 2 && v < 0x80) || (bytes == 3 && v < 0x800) ||
            (bytes == 4 && v < 0x10000) || (v >= 0xd800 && v <= 0xdfff) || v > 0x10ffff) {
            out[n++] = 0xfffd;
            ++p;
        } else {
            out[n++] = v;
            p += bytes;
        }
    }
    return n;
}
#endif
