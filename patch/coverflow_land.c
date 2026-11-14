/* Classic's landing: a song picked on Cover Flow's flip panel turns the panel over and lands it as
 * Now Playing's art (docs/internals.md, the flip). Its path. */
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
