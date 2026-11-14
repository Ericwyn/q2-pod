/* Classic's landing: a song picked on Cover Flow's flip panel turns the panel over and lands it as
 * Now Playing's art (docs/internals.md, the flip). Its path and its frames. */
#include "coverflow.h"

/* LAND_LEAN is the turn at which the flip's own projection gives slant_render's far edge,
 * asin(FLIP_EYE (1 - f) / (1 + f) / (NP_IPOD_ART / 2)) degrees, f = SLANT_FAR / 256; it and its
 * sine and cosine are constants, as the payload has sinf and cosf but no asin. */
#define LAND_LEAN 31.624f
#define LAND_LEAN_SIN .52434457f
#define LAND_LEAN_COS .85150618f

/* Cubic ease-out, x clamped to 0 to 1. */
float land_ease(float x) {
    x = x < 0 ? 0 : x > 1 ? 1 : x;
    return 1 - (1 - x) * (1 - x) * (1 - x);
}

/* A face w x h about centre (cx, cy), turned deg (as sine sn, cosine cs) about its vertical axis,
 * seen from FLIP_EYE: its TL, TR, BR, BL into q, the left edge at -w/2 of the face's own. */
static void land_face(float cx, float cy, float w, float h, float sn, float cs, float q[8]) {
    float xs = cs * w / 2, zs = sn * w / 2, kl = FLIP_EYE / (FLIP_EYE + zs),
          kr = FLIP_EYE / (FLIP_EYE - zs);
    q[0] = cx - xs * kl, q[1] = cy - h / 2 * kl, q[2] = cx + xs * kr, q[3] = cy - h / 2 * kr;
    q[4] = q[2], q[5] = cy + h / 2 * kr, q[6] = q[0], q[7] = cy + h / 2 * kl;
}

/* The landing at ms of len: the front face's TL, TR, BR, BL into q, its turn into *deg. from is the
 * panel's box (x, y, w, h), to the art's (x, y, side). The centre travels by travel percent of the
 * time; size and turn ease over all of it, from 180 (the back, as the panel shows it) on the flip
 * open's way round to 360 - LAND_LEAN. The same turn at the art's place and size falls short of
 * slant_render's keystone by a little, so each corner gains that shortfall in step with the turn,
 * and lands on it. test/coverflow.py */
void land_quad(const int from[4], const int to[3], int ms, int len, int travel, float q[8],
               float *deg) {
    float x = (float)ms / len, m = land_ease(x * 100 / travel), u = land_ease(x), s = to[2],
          i = s * (256 - SLANT_FAR) / 512, end[8], d = 180 + (180 - LAND_LEAN) * u;
    float ax = to[0] + s / 2, ay = to[1] + s / 2, px = from[0] + from[2] / 2.f,
          py = from[1] + from[3] / 2.f, th = d * 3.14159265f / 180;
    const float ks[8] = { to[0],     to[1],         to[0] + s, to[1] + i,
                          to[0] + s, to[1] + s - i, to[0],     to[1] + s };
    land_face(px + (ax - px) * m, py + (ay - py) * m, from[2] + (s - from[2]) * u,
              from[3] + (s - from[3]) * u, sinf(th), cosf(th), q);
    land_face(ax, ay, s, s, -LAND_LEAN_SIN, LAND_LEAN_COS, end);
    for (int j = 0; j < 8; ++j) q[j] += (ks[j] - end[j]) * u;
    *deg = d;
}

/* The landing's frame at ms into d (CF_VIEW_W x FLIP_H). Before np_at, 1, opaque, all of it: the
 * paper, the shelf parted as the flip leaves it and faded to the paper from out_at over out_ms, the
 * face and its reflection. Then 0: in box only the face and its reflection, over alpha 0 (straight
 * alpha); *veil the paper's alpha over Now Playing as it comes in over np_ms. From len, 2:
 * slant_render's art at to, box its rect, opaque. The face as coverflow_flip_render draws it, a
 * column at a time in 1/256 px: its left and right edges are upright, so each column's top and
 * bottom lie on the lines between them, u by one divide (u h is linear across, h being 1 / depth),
 * nearest texel, coverage at the ends and edges. The texture's first column is on the screen's
 * left for either face (the back's left is the front's right). The reflection, the front's only,
 * mirrors each column about its bottom, as slant_render's, its alpha growing with the turn to
 * slant_render's at the lean. test/coverflow.py */
int land_frame(unsigned *d, int pitch, int ms, const land_t *L, int box[4], int *veil) {
    int np = ms - L->np_at, opaque = np < 0, side = L->to[2];
    *veil = opaque ? 255 : np >= L->np_ms ? 0 : 255 - 255 * np / L->np_ms;
    if (ms >= L->len) {
        box[0] = L->to[0], box[1] = L->to[1], box[2] = side;
        box[3] = side + (side * SLANT_REFLECT_PCT + 99) / 100;
        slant_render(d + box[1] * pitch + box[0], pitch, side, box[3], L->front, ART_SIZE,
                     L->paper & 255);
        return 2;
    }
    float q[8], deg;
    land_quad(L->from, L->to, ms, L->len, L->travel, q, &deg);
    float cs = cosf(deg * 3.14159265f / 180);
    int back = cs < 0, i = back ? 2 : 0, j = 2 - i, tw = back ? BACK_W : ART_SIZE;
    int th = back ? BACK_H : ART_SIZE,
        ra = cs <= 0 ? 0 : (int)(SLANT_REFLECT_A * cs / LAND_LEAN_COS);
    ra = ra > SLANT_REFLECT_A ? SLANT_REFLECT_A : ra;
    /* the screen's left edge (x0, its top t0, bottom b0) and right (x1, t1, b1), 1/256 px */
    int x0 = (int)(q[i] * 256 + .5f), t0 = (int)(q[i + 1] * 256 + .5f),
        b0 = (int)(q[7 - i] * 256 + .5f), x1 = (int)(q[j] * 256 + .5f),
        t1 = (int)(q[j + 1] * 256 + .5f), b1 = (int)(q[7 - j] * 256 + .5f), w = x1 - x0;
    const unsigned *t = back ? L->back : L->front;
    int lo = t0 < t1 ? t0 : t1, hi = b0 + (ra ? (b0 - t0) * SLANT_REFLECT_PCT / 100 : 0),
        hr = b1 + (ra ? (b1 - t1) * SLANT_REFLECT_PCT / 100 : 0);
    hi = hi > hr ? hi : hr;
    box[0] = x0 >> 8, box[1] = lo >> 8, box[2] = (x1 + 255) >> 8, box[3] = (hi + 255) >> 8;
    box[0] = box[0] < 0 ? 0 : box[0] > CF_VIEW_W ? CF_VIEW_W : box[0];
    box[1] = box[1] < 0 ? 0 : box[1] > FLIP_H ? FLIP_H : box[1];
    box[2] = (box[2] > CF_VIEW_W ? CF_VIEW_W : box[2]) - box[0];
    box[3] = (box[3] > FLIP_H ? FLIP_H : box[3]) - box[1];
    box[2] = box[2] < 0 ? 0 : box[2], box[3] = box[3] < 0 ? 0 : box[3]; /* off the frame, edge on */
    if (opaque) {
        int f = ms <= L->out_at               ? 0
                : ms >= L->out_at + L->out_ms ? 256
                                              : 256 * (ms - L->out_at) / L->out_ms;
        for (int y = 0; y < FLIP_H; ++y) {
            unsigned *o = d + y * pitch;
            for (int x = 0; x < CF_VIEW_W; ++x) {
                int at = x < CF_CX ? x + FLIP_PART : x - FLIP_PART; /* parted */
                o[x] =
                    L->shelf && y < cfs.view_h && (x < CF_CX - FLIP_PART || x >= CF_CX + FLIP_PART)
                        ? tilt_mix(L->shelf[y * CF_VIEW_W + at], L->paper, (unsigned)f) |
                              0xff000000u
                        : L->paper;
            }
        }
    } else
        for (int y = box[1]; y < box[1] + box[3]; ++y)
            memset(d + y * pitch + box[0], 0, (unsigned)box[2] * 4);
    if (w < 16 || !t) return opaque; /* edge on */
    for (int x = box[0]; x < box[0] + box[2]; ++x) {
        int a = x << 8 > x0 ? x << 8 : x0, b = (x + 1) << 8 < x1 ? (x + 1) << 8 : x1, cx = b - a;
        int s = (((a + b) >> 1) - x0) * 4096 / w; /* across, 12 bits */
        int y0 = t0 + ((t1 - t0) * s >> 12), y1 = b0 + ((b1 - b0) * s >> 12), h = y1 - y0;
        if (cx <= 0 || h < 256) continue;
        int col = (s * ((b1 - t1) >> 4) / (h >> 4)) * tw >> 12, step = (th << 20) / (h >> 4);
        int fade = h * SLANT_REFLECT_PCT / 100, end = ra ? y1 + fade : y1;
        col = col < tw ? col : tw - 1;
        int y = y0 >> 8 < 0 ? 0 : y0 >> 8, last = (end - 1) >> 8;
        for (; y <= last && y < FLIP_H; ++y) {
            int p = y << 8, f0 = p > y0 ? p : y0, f1 = p + 256 < y1 ? p + 256 : y1;
            int r0 = p > y1 ? p : y1, r1 = p + 256 < end ? p + 256 : end, wf = 0, wr = 0;
            unsigned cf = 0, cr = 0, *o = d + y * pitch + x;
            if (f1 > f0) { /* the face's, its rows a step apart */
                int v = ((p + 128 - y0) >> 4) * step >> 20;
                cf = t[(v < 0 ? 0 : v < th ? v : th - 1) * tw + col];
                wf = (f1 - f0) * cx >> 8;
            }
            if (r1 > r0) { /* the reflection's, as far under the bottom as the texel is above it */
                int v = ((p + 128 - y1) >> 4) * step >> 20;
                cr = t[(th - 1 - (v < 0 ? 0 : v < th ? v : th - 1)) * tw + col];
                wr = (r1 - r0) * (ra * (fade - ((r0 + r1) >> 1) + y1) / fade) >> 8;
                wr = wr * cx >> 8;
            }
            if (opaque)
                *o = (shade(*o, (unsigned)(256 - wf - wr)) + shade(cf, (unsigned)wf) +
                      shade(cr, (unsigned)wr)) |
                     0xff000000u;
            else if (wf + wr) /* straight alpha: the colour, its share */
                *o = ((wr ? wf ? tilt_mix(cf, cr, (unsigned)(wr * 256 / (wf + wr))) : cr : cf) &
                      0xffffffu) |
                     (unsigned)(wf + wr > 255 ? 255 : wf + wr) << 24;
        }
    }
    return opaque;
}
