/* Crossfade (docs/internals.md#crossfade): Audio settings' row, its page and the fade hciplayer's
 * filter applies (peq_player.c). The page is stock's Boot volume page re-dressed: its slider runs
 * Off (0) and XF_MIN..XF_MAX seconds, so it needs no switch; the switch and Centre still turn it on
 * and off. Display's Wheel sensitivity row opens the same page (xf.wheel): 50-200% in tens, no
 * switch, the wheel a ten a tick and Centre back, as on Brightness (navigation.c edit_key). */
#include "offsets.inc"
#include "peq.h"
#include "playback.h"
#include "stock.h"

#define XF_MIN 1
#define XF_MAX 10
#define XF_DEFAULT 5
#define XF_ROW "label_xfade"    /* the Audio settings row's label, found again by name */
#define WHEEL_ROW "label_wheel" /* and Display's Wheel sensitivity row's */

static struct {
    int read, on, seconds, attached, done, key[4];
    int wheel; /* the page is Wheel sensitivity's */
    volatile int *flag;
    void *page, *sw, *view, *slider, *dec, *add, *label;
    unsigned press, press_at;
} xf __attribute__((section(".scratch")));

/* Q2POD XFADE (0, 1) and XFADESEC (XF_MIN..XF_MAX) in config.ini, read once. */
static void settings(void) {
    if (xf.read) return;
    char v[16] = "";
    xf.on = config_value("Q2POD", "XFADE", 2);
    toolsReadConfig("/mnt/data/config.ini", "Q2POD", "XFADESEC", v, "0");
    int n = atoi(v);
    xf.seconds = n >= XF_MIN && n <= XF_MAX ? n : XF_DEFAULT;
    xf.read = 1;
}

static void row_text(void *label) {
    char t[32] = "交叉淡化：关闭";
    if (xf.on) tk_snprintf(t, sizeof t, "交叉淡化：%d s", xf.seconds);
    widget_set_text_utf8(label, t);
}

/* Audio settings' row (navigation.c ringnav_audioset makes it): the label, named for refresh(). */
void xfade_row(void *label) {
    settings();
    widget_set_name(label, XF_ROW);
    row_text(label);
}

static void wheel_text(void *label) {
    char t[32];
    tk_snprintf(t, sizeof t, "滚轮灵敏度：%d%%", wheel_pct());
    widget_set_text_utf8(label, t);
}

/* Display's row (navigation.c ringnav_display makes it): the label, named for refresh(). */
void wheel_row(void *label) {
    widget_set_name(label, WHEEL_ROW);
    wheel_text(label);
}

/* The slider's value: the sensitivity, or 0 for Off, else the length. */
static int value(void) { return xf.wheel ? wheel_pct() : xf.on ? xf.seconds : 0; }

/* The page and the row from xf, and the choice saved. */
static void refresh(int save) {
    if (save && !xf.wheel) {
        write_int_config(xf.on, "Q2POD", "XFADE");
        write_int_config(xf.seconds, "Q2POD", "XFADESEC");
    }
    void *row = widget_lookup(window_manager(), XF_ROW, 1);
    if (row) row_text(row);
    if ((row = widget_lookup(window_manager(), WHEEL_ROW, 1))) wheel_text(row);
    if (!xf.page) return;
    char t[16];
    if (xf.wheel)
        tk_snprintf(t, sizeof t, "%d%%", wheel_pct());
    else {
        tk_snprintf(t, sizeof t, xf.on ? "%d s" : "关闭", xf.seconds);
        image_base_set_image(xf.sw, xf.on ? "switch_on" : "switch_off");
    }
    widget_set_text_utf8(xf.label, t);
    if (widget_get_prop_int(xf.slider, "value", 0) != value()) slider_set_value(xf.slider, value());
}

static void set_value(int n) {
    if (xf.wheel) {
        wheel_set_pct(n); /* rounds, clamps and saves */
        refresh(0);
        return;
    }
    n = n < 0 ? 0 : n > XF_MAX ? XF_MAX : n;
    if (n == value()) return;
    xf.on = n > 0;
    if (n) xf.seconds = n < XF_MIN ? XF_MIN : n;
    refresh(1);
}

static int toggle(void *ctx, void *event) {
    if (xf.wheel) return 0;
    xf.on = !xf.on;
    refresh(1);
    return 0;
}
static int step(void *ctx, void *event) {
    set_value(value() + (int)(long)ctx * (xf.wheel ? 10 : 1));
    return 0;
}
static int slid(void *ctx, void *event) {
    set_value(widget_get_prop_int(xf.slider, "value", value()));
    return 0;
}
static int gone(void *ctx, void *event) {
    if (xf.page == ctx) xf.page = 0, xf.wheel = 0;
    return 0;
}

/* The row's click: stock's Boot volume page, its handlers swapped for these; ctx 1 for Wheel
 * sensitivity's row. */
int xfade_open(void *ctx, void *event) {
    int wheel = ctx != 0;
    settings();
    if (xf.page) return 0;
    void *wm = window_manager(), *below = window_manager_get_top_window(wm);
    navigator_to("playset/bootvol_page");
    void *page = window_manager_get_top_window(wm);
    if (page == below) return 0;
    void *nav = widget_lookup(page, "view_navbar", 1), *title = nav ? widget_get_child(nav, 0) : 0;
    xf.sw = widget_lookup(page, "img_bootswitch", 1);
    xf.view = widget_lookup(page, "view_bootvol", 1);
    xf.slider = widget_lookup(page, "slider_bootvol", 1);
    xf.dec = widget_lookup(page, "img_dec", 1);
    xf.add = widget_lookup(page, "img_add", 1);
    xf.label = widget_lookup(page, "label_vol", 1);
    if (tk_strcmp(widget_get_prop_str(page, "name", ""), "bootvol_page") || !title || !xf.sw ||
        !xf.view || !xf.slider || !xf.dec || !xf.add || !xf.label) {
        window_close(page);
        return 0;
    }
    widget_off_by_func(xf.sw, EVT_CLICK, (void *)BOOTVOL_SWITCH, page);
    widget_off_by_func(xf.dec, EVT_CLICK, (void *)BOOTVOL_DEC, page);
    widget_off_by_func(xf.add, EVT_CLICK, (void *)BOOTVOL_ADD, page);
    widget_off_by_func(xf.slider, EVT_VALUE_CHANGING, (void *)BOOTVOL_CHANGING, page);
    widget_off_by_func(xf.slider, EVT_VALUE_CHANGED, (void *)BOOTVOL_CHANGED, page);
    widget_on(xf.sw, EVT_CLICK, toggle, 0);
    widget_on(xf.dec, EVT_CLICK, step, (void *)-1L);
    widget_on(xf.add, EVT_CLICK, step, (void *)1L);
    widget_on(xf.slider, EVT_VALUE_CHANGING, slid, 0);
    widget_on(xf.slider, EVT_VALUE_CHANGED, slid, 0);
    widget_on(page, EVT_DESTROY, gone, page);
    widget_set_text_utf8(title, wheel ? "滚轮灵敏度" : "交叉淡化");
    widget_set_visible(xf.view, 1, 0); /* stock shows it only while Boot volume's switch is on */
    if (wheel) widget_set_visible(xf.sw, 0, 0);
    slider_set_min(xf.slider, wheel ? 50 : 0);
    slider_set_max(xf.slider, wheel ? 200 : XF_MAX);
    slider_set_step(xf.slider, wheel ? 10 : 1);
    xf.wheel = wheel;
    xf.page = page;
    refresh(0);
    return 0;
}

/* A single centre press, DOUBLE_CLICK_MS on: the switch. */
static int single(const void *info) {
    xf.press = 0;
    toggle(0, 0);
    return 0;
}

/* ringnav(), the page on top: Centre toggles, a double press turns the screen off as on every
 * page, and the wheel sets the length, down to Off. Wheel sensitivity: the wheel a ten a tick
 * (not through +/-, which stock may have disabled), Centre to edit_key, which goes back. */
int xfade_key(void *top, unsigned key) {
    static const unsigned release[EVENT_KEY / 4 + 1] = { [EVENT_KEY / 4] = KEY_CENTER };
    if (!xf.page || top != xf.page) return 0;
    if (key == KEY_CENTER) {
        if (xf.wheel) return 0;
        int press = center_press(&xf.press, &xf.press_at, single, 0);
        if (press == 2 && g_backlight_status) on_wm_keyup_fun((void *)0, (void *)release);
        return press;
    }
    if (key != KEY_NEXT && key != KEY_PREV) return 0;
    set_value(value() + (key == KEY_NEXT ? 1 : -1) * (xf.wheel ? 10 : 1));
    return 1;
}

/* playback_poll, each UI pass: the next change's fade into XFADE_FILE, made whole under another
 * name first as the visualizer's tap is. A fade only when Crossfade and stock's Gapless are on and
 * the next track is another album's (album_cmp), so albums stay gapless. */
void xfade_poll(int next) {
    void *q = P(mcl_pdeqplaylist, 0);
    unsigned pos = (unsigned)I(MCL_POS, 0), n = q ? deque_size(q) : 0;
    if (!xf.flag && (next < 0 || pos >= n)) return; /* nothing playing on: no config read yet */
    settings();
    int in = next >= 0 && pos < n && (unsigned)next < n; /* records, not positions: a new queue */
    int key[4] = { xf.on ? xf.seconds : 0, *(volatile unsigned char *)MCL_GAPLESS,
                   in ? (int)deque_at(q, pos) : 0, in ? (int)deque_at(q, (unsigned)next) : 0 };
    if (xf.done && !memcmp(key, xf.key, sizeof key)) return;
    memcpy(xf.key, key, sizeof key);
    int ms = key[0] && key[1] && key[2] != key[3] && album_cmp((void *)key[2], (void *)key[3])
                 ? key[0] * 1000
                 : 0;
    xf.done = 0; /* until the player is told: a file that cannot be made yet is tried next pass */
    if (!xf.flag && ms) {
        int fd = open(XFADE_FILE ".tmp", 0x302, 0644); /* O_RDWR | O_CREAT | O_TRUNC (MIPS) */
        if (fd < 0) return;
        if (!ftruncate(fd, sizeof *xf.flag) && !rename(XFADE_FILE ".tmp", XFADE_FILE)) {
            void *p = mmap(0, sizeof *xf.flag, 3, 1, fd, 0); /* PROT_READ | PROT_WRITE, MAP_SHARED */
            if (p != (void *)-1) xf.flag = p;
        }
        close(fd);
        if (!xf.flag) return;
    }
    if (xf.flag) *xf.flag = ms;
    xf.done = 1;
    /* The fade runs in the PEQ filter, which boot leaves out of the chain while PEQ is off. */
    if (ms && !xf.attached && !g_equalizer_flag) peq_attach();
    xf.attached |= ms != 0;
}
