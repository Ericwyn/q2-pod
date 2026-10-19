#include "peq.h"
#include "offsets.inc"

extern int stock_eq_trampoline(int mode);
int peq_chained __attribute__((section(".scratch"))); /* a peq_stock_eq this boot: the filter is in */
/* All calls, including restored playback, instantiate the replacement even in bypass.
 * Afterwards the flag only drives the status-bar EQ icon (systembar_showface @0x52f8b0): on
 * while 3EQ or PEQ is. */
int peq_stock_eq(int mode) {
    peq_chained = 1;
    g_equalizer_flag = 1;
    int result = stock_eq_trampoline(1);
    eq_state s;
    eq_load(&s);
    g_equalizer_flag = s.mode != EQ_OFF;
    return result;
}

/* Every EQ change goes through here: s saved, the stock switch's config key (boot instantiates
 * the filter only when it is set) written when on/off changes, and the player told. */
static int eq_commit(eq_state *s) {
    if (!eq_save(s)) return 0;
    int on = s->mode != EQ_OFF;
    if (on != g_equalizer_flag) write_int_config(on, "PLAYSET", "EQFLAG");
    peq_stock_eq(1);
    return 1;
}

/* EQ off but its filter in the chain, for the visualizer's tap (visualizer.c): the switch's Off, so a
 * mode saved on while the stock flag is clear stays silent; nothing if that cannot be saved. */
void peq_attach(void) {
    eq_state s;
    eq_load(&s);
    if (s.mode == EQ_OFF || (s.mode = EQ_OFF, s.hold = 0, eq_save(&s))) peq_stock_eq(1);
}

enum { HOME, BAND, PRESETS, IMPORTS, SAVES, CONFIRM, DELETES, PICK, ADJUST, STOCKS };
enum { BACK = 1000, APPLY, RESET, BYPASS, MENU, IMPORT, SAVE, ENABLE, TYPE, RAISE, LOWER, TYPED, CHANNEL, STEP,
       YES, CANCEL, DELETE, BALANCE, PREAMP, GAIN, /* these three open the picker, in picks[] order */
       FREQUENCY, QUALITY, /* and these two the value menu */ KEYBOARD, STOCK, FILE_PREV, FILE_NEXT };
#define FILE_PAGE 128
#define GRAPH_W (375 - 2 * PEQ_GRAPH_X)
#if IPOD
#define ROW_X CF_EDGE /* the text column clear of the rounded glass, as coverflow.c's */
#define TONE accent_tone(2) /* the light tone */
#else
#define ROW_X 12
#define TONE 0xffffff
#endif
/* The page's two tabs: Simple, 3EQ's sliders (e3), and Advanced, the PEQ editor (ui.screen). */
enum { SIMPLE, ADVANCED };
static struct {
    void *page, *view, *edit, *graph, *bar; /* bar: the title, which shows the tabs on the top screens */
    unsigned timer;
    int screen, band, step, count, previous, rendered, dirty, pick, adjust, tries;
    int capacity, file_page;
    int tab, tabs; /* tabs: the wheel is on the tab bar, over Advanced's list */
    double typed;
    peq_preset draft, candidate, named; /* named: the preset applied under applied's name */
    char (*names)[256];
    char destination[600], status[160];
    char name[64], applied[64]; /* the draft's preset, and the active one's, when loaded from a file */
    float curve[2][GRAPH_W];      /* the left and right response, in graph rows */
} ui __attribute__((section(".scratch")));

static const int steps[] = {1, 10, 100, 1000};
/* Wheel pickers in 0.5 dB rows: row i is first + step * i. Preamp's row 0 is Auto (first is unused there). */
static const struct { double first, step; int rows; } picks[] = {{-12, 0.5, 49}, {12.5, -0.5, 74}, {24, -0.5, 97}};
/* Simple, 3EQ (docs/internals.md#3eq): Low, Mid and High, -6 to +6 dB in 0.5 dB steps, on a view
 * that e3_paint draws: the switch and Reset under the tabs, then a slider per band over a 0 dB line.
 * The wheel moves between the tabs and the five; Centre flips the switch, resets, or takes a slider, whose gain
 * the wheel then turns until Centre or Return. A touch on a slider drags it. Edits are saved at
 * most every E3_SAVE_MS, and at once when an adjustment ends; the player hears them only while
 * 3EQ is on, which turns PEQ off. */
#define E3_MAX 6
#define E3_BUTTONS 52 /* the switch and Reset, 40px high */
#define E3_SLIDERS 104
#define E3_SAVE_MS 250
enum { E3_TABS, E3_SWITCH, E3_LOW, E3_HIGH = 4, E3_RESET };
static struct {
    void *view;
    unsigned save, press, press_at;
    int focus, adjusting, dragging, dirty, on, h;
    double gain[3];
    const char *status;
} e3 __attribute__((section(".scratch")));

static int action(void *ctx, void *event);
static int render(const void *unused);

/* A row: the caption on the left, white or, for display-only rows and off bands (dim), grey, and the
 * value, if any, in grey against the right edge. */
static void row_dim(void *view, int index, const char *text, const char *value, int id, int dim) {
    void *item = list_item_create(view, 0, index * 48, 375, 48);
    widget_use_style(item, "s_listitem_black");
    for (int i = 0; i < 1 + !!value; ++i) {
        void *label = label_create(item, ROW_X, 0, 375 - 2 * ROW_X, 48);
        widget_use_style(label, "s_label_white20l");
        if (i || dim || id < 0) widget_set_prop_int(label, "style:normal:text_color", (int)CF_GREY);
        if (i) widget_set_prop_str(label, "style:normal:text_align_h", "right");
        widget_set_text_utf8(label, i ? value : text);
    }
    widget_on(item, EVT_CLICK, action, (void *)(long)id);
}
#define row(view, index, text, id) row_dim(view, index, text, 0, id, 0)

/* |H|^2 of the engine's left and right chains, without preamp or balance. */
static void response(const peq_engine *e, int rate, double f, double *l, double *r) {
    double w = 6.283185307179586 * f / rate, c1 = cos(w), s1 = sin(w), c2 = 2 * c1 * c1 - 1, s2 = 2 * s1 * c1;
    *l = *r = 1;
    for (int i = 0; i < e->used; ++i) { /* bands past used are identity */
        const peq_coeff *q = &e->c[i];
        double nr = q->b0 + q->b1 * c1 + q->b2 * c2, ni = q->b1 * s1 + q->b2 * s2;
        double dr = 1 + q->a1 * c1 + q->a2 * c2, di = q->a1 * s1 + q->a2 * s2;
        double m = (nr * nr + ni * ni) / (dr * dr + di * di);
        if (e->only[i] != 2) *l *= m; /* only: 1 left, 2 right */
        if (e->only[i] != 1) *r *= m;
    }
}

static float graph_y(double db) {
    if (db > PEQ_GRAPH_DB) db = PEQ_GRAPH_DB;
    if (db < -PEQ_GRAPH_DB) db = -PEQ_GRAPH_DB;
    return (float)((PEQ_PLOT_TOP + PEQ_PLOT_BOTTOM) / 2.0 - db * (PEQ_PLOT_BOTTOM - PEQ_PLOT_TOP) / (2.0 * PEQ_GRAPH_DB));
}
static float graph_x(double f) { return (float)(PEQ_GRAPH_X + (GRAPH_W - 1) * log(f / 20) / log(1000)); }

/* The draft's response, one point a column, for peq_paint. */
static void curve(void) {
    peq_engine e;
    int ok = peq_compile(&ui.draft, 48000, &e);
    for (int x = 0; x < GRAPH_W; ++x) {
        double l = 1, r = 1;
        if (ok) response(&e, 48000, 20 * pow(1000, x / (GRAPH_W - 1.0)), &l, &r);
        ui.curve[0][x] = graph_y(decibels(l));
        ui.curve[1][x] = graph_y(decibels(r));
    }
}

static double response_peak(const peq_engine *e, int rate, double f) {
    double l, r;
    response(e, rate, f, &l, &r);
    return l > r ? l : r;
}

/* Auto is a saved number, so cover standard PCM rates instead of tying it to the current track.
 * shortcut: sampled steady-state peaks, not a transient limiter or a bound for nonstandard rates. */
static double headroom(const peq_preset *p) {
    static const int rates[] = {8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100,
                               48000, 64000, 88200, 96000, 176400, 192000, 352800, 384000};
    static peq_preset cached;
    static int have_cache;
    static double gain;
    if (have_cache && !memcmp(p, &cached, sizeof(*p))) return gain;
    peq_engine e;
    double peak = 1;
    for (unsigned i = 0; i < sizeof(rates) / sizeof(*rates); ++i) {
        int rate = rates[i];
        if (!peq_compile(p, rate, &e)) return p->preamp;
        double nyquist = rate * 0.5, best = 0;
        /* Include DC, Nyquist and each band's centre: near Nyquist a peak can be narrower than the grid. */
        for (int k = -2; k < 1024 + p->count; ++k) {
            double f = k == -2 ? 0 : k == -1 ? nyquist : k < 1024 ? pow(nyquist, k / 1023.0)
                       : p->bands[k - 1024].frequency;
            if (f >= nyquist && k != -1) continue;
            double v = response_peak(&e, rate, f);
            if (v > best) best = v;
        }
        if (best > peak) peak = best;
    }
    gain = -decibels(peak);
    gain = gain > -0.0001 ? 0 : gain < -60 ? -60 : gain;
    cached = *p;
    have_cache = 1;
    return gain; /* no boost: +0, not -0.0 on screen */
}

static double *picked(void) {
    return (double *[]){&ui.draft.balance, &ui.draft.preamp, &ui.draft.bands[ui.band].gain}[ui.pick - BALANCE];
}

/* The value menu's edit closed with new text: apply it if it is a number (clamped like Raise/Lower), else restore it. */
static int typed(void *ctx, void *event) {
    char s[16];
    if (!ui.edit || widget_get_text_utf8(ui.edit, s, sizeof s)) s[0] = 0;
    if (peq_number(s, &ui.typed)) action((void *)(long)TYPED, 0);
    else if (!ui.timer) ui.timer = timer_add(render, 0, 1); /* show the value again */
    return 0;
}

/* The T9 keyboard opens on its letters; a value only needs its "123" page (symnum). The keyboard can open a
 * moment after the focus, so look for its window every 16 ms, up to 20 times. */
static int numbers(const void *unused) {
    void *kb = widget_lookup(window_manager(), "kb_default_t9", 0);
    if (kb) pages_set_active_by_name(widget_lookup(kb, "panel", 1), "symnum");
    return kb || ++ui.tries >= 20 ? 7 : 8; /* RET_REMOVE, else RET_REPEAT */
}

static int focused(void *ctx, void *event) {
    ui.tries = 0;
    timer_add(numbers, 0, 16);
    return 0;
}

/* Auto unless the preamp was picked (2): imports and presets or states from before 1.3.6 take it. */
static int on_auto(void) { return ui.draft.automatic != 2; }

/* A draft on Auto takes the current headroom (an update can move it): 1 if that changed it. */
static int follow_auto(void) {
    double was = ui.draft.preamp;
    if (on_auto()) { /* headroom before the flag: the flag is part of its cache key */
        double h = headroom(&ui.draft);
        ui.draft.automatic = 1, ui.draft.preamp = h;
    }
    return ui.draft.preamp != was;
}

static int compare_names(const void *a, const void *b) { return strcmp(a, b); }

static void list_files(const char *folder, const char *extension) {
    ui.count = 0;
    void *dir = opendir(folder);
    if (!dir) { snprintf(ui.status, sizeof(ui.status), "文件夹不存在或不可用"); return; }
    struct dirent *entry;
    while ((entry = readdir(dir))) {
        const char *name = entry->d_name;
        unsigned n = strlen(name), e = strlen(extension);
        if (entry->d_type != 8 && entry->d_type != 0) continue;
        if (n <= e || n >= 256 || strcmp(name + n - e, extension)) continue;
        if (ui.count == ui.capacity) {
            int capacity = ui.capacity ? ui.capacity * 2 : FILE_PAGE;
            void *names = calloc(capacity, 256);
            if (!names) {
                ui.count = 0;
                snprintf(ui.status, sizeof(ui.status), "内存不足，无法列出预设");
                break;
            }
            if (ui.count) memcpy(names, ui.names, ui.count * 256);
            free(ui.names);
            ui.names = names;
            ui.capacity = capacity;
        }
        memcpy(ui.names[ui.count++], name, n + 1);
    }
    closedir(dir);
    if (ui.count) qsort(ui.names, ui.count, 256, compare_names);
    if (!ui.count && !ui.status[0]) snprintf(ui.status, sizeof(ui.status), "未找到预设");
}

/* File name in ui.destination: sizeof skips PEQ_SAVED and its "/". */
static const char *deleting(void) { return ui.destination + sizeof(PEQ_SAVED); }

/* Stock's genre presets, built in: reset_user_eq's table (modes 4-11), in dB over the ten default
 * bands. Picking one loads it into the editor as a saved preset does; nothing is written. */
static const char stock_names[][10] = { "流行", "摇滚", "舞曲", "布鲁斯", "金属", "人声", "古典", "爵士" };
static const signed char stock_gains[][10] = {
    { 3, 1, 0, -2, -4, -4, -2, 0, 1, 2 }, { -2, 0, 2, 4, -2, -2, 0, 0, 4, 4 },
    { -2, 3, 4, 1, -2, -2, 0, 0, 4, 4 },  { -2, 0, 2, 1, 0, 0, 0, 0, -2, -4 },
    { -6, 0, 0, 0, 0, 0, 2, 0, 2, 0 },    { -4, 0, 2, 1, 0, 0, 0, 0, -4, -6 },
    { 0, 3, 3, 2, 0, 0, 0, 0, 1, 1 },     { 0, 0, 0, 2, 2, 2, 0, 1, 2, 2 },
};
#define STOCK_N (int)(sizeof(stock_names) / sizeof(*stock_names))

/* Into the editor, not active: Apply is the activation step. */
static void load_draft(peq_preset *p, const char *name, int length) {
    p->bypass = ui.draft.bypass; /* ON/OFF is live state, not part of the edit */
    ui.draft = *p;
    follow_auto();
    ui.dirty = 1;
    snprintf(ui.name, sizeof(ui.name), "%.*s", length, name);
    ui.screen = HOME;
    snprintf(ui.status, sizeof(ui.status), "预设已载入，请选择应用以启用");
}

/* Search fits above T9's screen y=60 (page y=30), so opening the keyboard won't shift the page. */
void *edit_create(void *item, const char *input_type, int y, int h) {
    void *edit = widget_factory_create_widget(widget_factory(), "edit", item, 12, y, 351, h);
    static const char *const props[][2] = {{"keyboard", "kb_default_t9"}, {"input_type", 0}, {"action_text", "done"},
        {"bg_color", "#2B2B2B"}, {"border_color", "#2B2B2B00"}, {"text_color", "#FFFFFF"}, {"round_radius", 0},
        {"margin_left", "12"}, {"font_size", "22"}};
    static const char *const states[] = {"normal", "focused", "empty", "empty_focus", "changed", "error", "over", "empty_over"};
    char name[48], radius[12];
    snprintf(radius, sizeof(radius), "%d", h / 2);
    for (unsigned i = 0; i < sizeof(props) / sizeof(props[0]); ++i) {
        if (i < 3) { widget_set_prop_str(edit, props[i][0], i == 1 ? input_type : props[i][1]); continue; }
        for (unsigned j = 0; j < sizeof(states) / sizeof(states[0]); ++j) {
            snprintf(name, sizeof(name), "style:%s:%s", states[j], props[i][0]);
            widget_set_prop_str(edit, name, props[i][1] ? props[i][1] : radius);
        }
    }
    return edit;
}

static void save_candidate(int replace) {
    mkdir(PEQ_SAVED, 0700);
    int result = peq_save(ui.destination, &ui.candidate, replace);
    if (result == 2) { ui.previous = ui.screen; ui.screen = CONFIRM; }
    else snprintf(ui.status, sizeof(ui.status), result ? "预设已保存，当前均衡器未变" : "保存失败，设置未变");
}

static int action(void *ctx, void *event) {
    int id = (int)(long)ctx;
    if (id < 0) return 0; /* display-only rows */
    int screen = ui.screen;
    /* The centre button on the value row; no render, which would destroy the edit and close the keyboard. */
    if (id == KEYBOARD) { widget_set_focused(ui.edit, 1); return 0; }
    peq_band *b = &ui.draft.bands[ui.band];
    ui.status[0] = 0;
    ui.tabs = 0;
    /* Band edits keep the preamp on Auto (just enough cut for the boosts) only if it was there. */
    int edit = (id >= ENABLE && id < STEP) || (id < 256 && ui.screen == PICK && ui.pick == GAIN);
    if (id == BACK) {
        if (ui.screen == HOME) { navigator_back(); return 0; }
        if (ui.screen == PICK) ui.screen = ui.pick == GAIN ? BAND : HOME;
        else if (ui.screen == ADJUST) ui.screen = BAND;
        else ui.screen = ui.screen == BAND || ui.screen == PRESETS ? HOME : PRESETS;
    } else if (id == MENU) ui.screen = PRESETS;
    else if (id == IMPORT) ui.screen = IMPORTS;
    else if (id == FILE_PREV || id == FILE_NEXT) {
        ui.file_page += id == FILE_NEXT ? 1 : -1;
        ui.rendered = -1;
    }
    else if (id == STOCK) ui.screen = STOCKS;
    else if (id == DELETE) ui.screen = DELETES;
    else if (id >= BALANCE && id <= GAIN) { ui.pick = id; ui.screen = PICK; }
    else if (id == FREQUENCY || id == QUALITY) { ui.adjust = id; ui.screen = ADJUST; }
    else if (id == SAVE) ui.screen = SAVES;
    else if (id == BYPASS) {
        /* Takes effect at once and keeps unapplied band edits out of the active preset. On turns 3EQ off. */
        eq_state s;
        eq_load(&s);
        s.mode = ui.draft.bypass ? EQ_PEQ : EQ_OFF;
        s.hold = 0;
        if (eq_commit(&s)) ui.draft.bypass = s.mode != EQ_PEQ;
        else snprintf(ui.status, sizeof(ui.status), "切换失败，均衡器未变");
    }
    else if (id == ENABLE) b->enabled = !b->enabled;
    else if (id == TYPE) b->type = (b->type + 1) % 3;
    else if (id == CHANNEL && b->enabled) b->enabled = b->enabled % 3 + 1; /* both, left, right */
    else if (id == STEP) ui.step = (ui.step + 1) % 4;
    else if (id == RAISE || id == LOWER || id == TYPED) {
        int k = ui.adjust == QUALITY;
        double *value = k ? &b->q : &b->frequency, step = k ? 0.05 : steps[ui.step];
        *value = id == TYPED ? ui.typed : *value + (id == RAISE ? step : -step);
        if (*value < (k ? 0.1 : 20)) *value = k ? 0.1 : 20;
        if (*value > (k ? 10 : 20000)) *value = k ? 10 : 20000;
    } else if (id == APPLY) {
        eq_state s;
        eq_load(&s);
        s.peq = ui.draft;
        if (!ui.draft.bypass) s.mode = EQ_PEQ; /* the draft shows On only while PEQ plays */
        if (eq_commit(&s)) {
            ui.dirty = 0;
            memcpy(ui.applied, ui.name, sizeof(ui.name));
            ui.named = ui.draft;
            snprintf(ui.status, sizeof(ui.status), "已应用");
        } else snprintf(ui.status, sizeof(ui.status), "应用失败，当前均衡器未变");
    } else if (id == RESET) {
        /* The draft returns to peq_default; On/Off is live state, not part of the edit. */
        int bypass = ui.draft.bypass;
        peq_default(&ui.draft);
        ui.draft.bypass = bypass;
        follow_auto();
        ui.name[0] = 0;
        ui.dirty = 1;
        snprintf(ui.status, sizeof(ui.status), "已重置，请选择应用以启用");
    } else if (id == YES) {
        ui.screen = ui.previous;
        if (ui.screen != DELETES) save_candidate(1);
        /* Saved presets are copies; the active EQ and the editor draft are untouched. */
        else if (unlink(ui.destination)) snprintf(ui.status, sizeof(ui.status), "删除失败，预设已保留");
        else snprintf(ui.status, sizeof(ui.status), "已删除 %.100s，当前均衡器未变", deleting());
    } else if (id == CANCEL) ui.screen = ui.previous;
    else if (id < 256) {
        int file = ui.file_page * FILE_PAGE + id;
        if (ui.screen == HOME && id < PEQ_BANDS) { ui.band = id; ui.screen = BAND; }
        else if (ui.screen == PICK && id < picks[ui.pick - BALANCE].rows) {
            /* Preamp's Auto row: the band-edit cut, which later band edits then keep up to date. */
            *picked() = ui.pick == PREAMP && !id ? headroom(&ui.draft) : picks[ui.pick - BALANCE].first + picks[ui.pick - BALANCE].step * id;
            if (ui.pick == PREAMP) ui.draft.automatic = id ? 2 : 1;
            if (ui.pick == GAIN && !b->enabled) b->enabled = 1; /* picking a gain is using the band */
            ui.dirty = 1;
            ui.screen = ui.pick == GAIN ? BAND : HOME;
        }
        else if (ui.screen == STOCKS && id < STOCK_N) {
            peq_preset p;
            peq_default(&p);
            for (int k = 0; k < 10; ++k) p.bands[k].enabled = (p.bands[k].gain = stock_gains[id][k]) != 0;
            p.automatic = 1; /* load_draft sets the preamp */
            load_draft(&p, stock_names[id], sizeof(stock_names[id]));
        }
        else if (ui.screen == SAVES && id < 10) {
            snprintf(ui.destination, sizeof(ui.destination), PEQ_SAVED "/Manual %02d.peq", id + 1);
            ui.candidate = ui.draft;
            save_candidate(0);
        } else if (file < ui.count && ui.screen == IMPORTS) {
            char path[600];
            peq_error error;
            snprintf(path, sizeof(path), PEQ_IMPORT "/%s", ui.names[file]);
            if (!peq_import_file(path, &ui.candidate, &error))
                snprintf(ui.status, sizeof(ui.status), "第 %u 行：%s", error.line, error.reason);
            else {
                unsigned n = strlen(ui.names[file]) - 4;
                snprintf(ui.destination, sizeof(ui.destination), PEQ_SAVED "/%.*s.peq", (int)n, ui.names[file]);
                save_candidate(0);
            }
        } else if (file < ui.count && ui.screen == DELETES) {
            snprintf(ui.destination, sizeof(ui.destination), PEQ_SAVED "/%s", ui.names[file]);
            ui.previous = DELETES;
            ui.screen = CONFIRM;
        } else if (file < ui.count && ui.screen == PRESETS) {
            char path[600];
            snprintf(path, sizeof(path), PEQ_SAVED "/%s", ui.names[file]);
            peq_preset p;
            if (peq_load(path, &p)) load_draft(&p, ui.names[file], (int)strlen(ui.names[file]) - 4);
            else snprintf(ui.status, sizeof(ui.status), "无法读取预设，设置未变");
        }
    }
    if (edit) { /* ENABLE, TYPE, RAISE, LOWER, TYPED, CHANNEL and a picked gain */
        ui.dirty = 1;
        follow_auto();
    }
    if (screen != ui.screen) ui.file_page = 0;
    if (!ui.timer) ui.timer = timer_add(render, 0, 1);
    return 0;
}

static void *bar(void *page, int x, int w);
static void e3_show(void);
static int e3_touch(void *ctx, void *event);

static int render(const void *unused) {
    ui.timer = 0;
    if (!ui.page) return 7;
    if (ui.tab == SIMPLE) { /* the sliders' view, under the tabs */
        ui.view = ui.graph = ui.edit = 0;
        ui.rendered = -1;
        widget_destroy_children(ui.page);
        e3.view = widget_factory_create_widget(widget_factory(), "view", ui.page, 0, 0, 375, e3.h);
        widget_on(e3.view, EVT_POINTER_DOWN, e3_touch, 0);
        ui.bar = bar(ui.page, 0, 375);
        e3_show();
        return 7;
    }
    e3.view = 0;
    int selection = 0, offset = 0;
    int height = widget_get_prop_int(ui.page, "h", 320), graph = ui.screen == HOME || ui.screen == BAND;
    int top = 48 + graph * PEQ_GRAPH_H;
    /* Whole rows only, so the last row is never clipped. */
    int rows = (height - top) / 48 * 48;
    if (ui.view && ui.rendered == ui.screen) {
        selection = widget_get_prop_int(ui.view, "_ringnav_index", 0);
        offset = widget_get_prop_int(ui.view, "yoffset", 0);
    } else if (ui.screen == PICK) { /* opens on the nearest row to the current value (Auto if on it), centred */
        int last = picks[ui.pick - BALANCE].rows - 1;
        double r = (*picked() - picks[ui.pick - BALANCE].first) / picks[ui.pick - BALANCE].step + 0.5;
        selection = r < 0 ? 0 : r > last ? last : (int)r;
        if (ui.pick == PREAMP && on_auto()) selection = 0;
        else if (ui.pick == PREAMP && !selection) selection = 1;
        offset = selection * 48 - (rows - 48) / 2;
        offset = offset < 0 ? 0 : offset > (last + 1) * 48 - rows ? (last + 1) * 48 - rows : offset;
    }
    ui.rendered = ui.screen;
    ui.edit = 0;
    widget_destroy_children(ui.page);
    void *list = list_view_create(ui.page, 0, top, 375, rows);
    widget_set_prop_int(list, "item_height", 48);
    /* The theme's default list_view is a light card; stock pages paint theirs black inline. */
    widget_set_prop_int(list, "style:normal:bg_color", (int)0xff000000u);
    widget_set_prop_int(list, "style:normal:border_color", 0);
    void *view = scroll_view_create(list, 0, 0, 375, rows);
    ui.view = view;
    widget_set_prop_int(view, "yslidable", 1);
    widget_set_prop_int(view, "xslidable", 0);
    widget_set_prop_int(view, "_ringnav_index", ui.tabs && ui.screen == HOME ? -1 : selection);
    char text[160], value[32];
    int n = 0;
    static const char *const types[] = {"峰值", "低架滤波", "高架滤波"};
    /* No Back row: Return steps back on every screen (keyup). */
    if (ui.screen == HOME) {
        row(view, n++, ui.dirty ? "应用更改" : "无待应用更改", ui.dirty ? APPLY : -1);
        row(view, n++, "恢复默认设置", RESET);
        row_dim(view, n++, "启用", ui.draft.bypass ? "关闭" : "打开", BYPASS, 0);
        /* Balance turns one side down: R 1.0 dB is the left 1 dB quieter. */
        snprintf(value, sizeof(value), __builtin_fabs(ui.draft.balance) >= 0.05 ? "%s %.1f dB" : "居中",
                 ui.draft.balance > 0 ? "R" : "L", __builtin_fabs(ui.draft.balance));
        row_dim(view, n++, "声道平衡", value, BALANCE, 0);
        snprintf(value, sizeof(value), on_auto() ? "%.1f dB (自动)" : "%.1f dB", ui.draft.preamp);
        row_dim(view, n++, "前级增益", value, PREAMP, 0);
        row_dim(view, n++, "预设", ui.name[0] ? ui.name : 0, MENU, 0); /* the draft's preset */
        /* Ten rows, then one spare past the last used band to add another. */
        for (int i = 0; i < PEQ_BANDS && (i < 10 || i <= ui.draft.count); ++i) {
            peq_band *b = &ui.draft.bands[i];
            snprintf(text, sizeof(text), "%d  %s %.0f Hz%s", i + 1, types[b->type], b->frequency,
                     (const char *[]){"", "", " L", " R"}[b->enabled]);
            snprintf(value, sizeof(value), b->enabled ? "%+.1f dB" : "关闭", b->gain);
            row_dim(view, n++, text, value, i, !b->enabled);
        }
    } else if (ui.screen == BAND) {
        peq_band *b = &ui.draft.bands[ui.band];
        /* Imported missing bands already have valid defaults from peq_default. */
        if (ui.draft.count <= ui.band) ui.draft.count = ui.band + 1;
        row_dim(view, n++, "频段", b->enabled ? "打开" : "关闭", ENABLE, 0);
        if (b->enabled) row_dim(view, n++, "声道", (const char *[]){0, "双声道", "左声道", "右声道"}[b->enabled], CHANNEL, 0);
        row_dim(view, n++, "类型", types[b->type], TYPE, 0);
        snprintf(value, sizeof(value), "%.0f Hz", b->frequency); row_dim(view, n++, "频率", value, FREQUENCY, 0);
        snprintf(value, sizeof(value), "%+.1f dB", b->gain); row_dim(view, n++, "增益", value, GAIN, 0);
        snprintf(value, sizeof(value), "%.2f", b->q); row_dim(view, n++, "Q", value, QUALITY, 0);
    } else if (ui.screen == ADJUST) {
        /* Row 0 is the value in an edit, styled and keyed as the stock playlist dialogs' (T9 keyboard, 123 page
         * for digits); a tap or the centre button opens the keyboard and the value applies when it closes. Its
         * action text is "done", as stock edit_on_event only acts on the keyboard's OK for "done" (close the
         * keyboard, then EVT_VALUE_CHANGED) or "next"; the dialogs' "OK" is handled by their own EVT_IM_ACTION
         * handlers. The keyboard's OK key shows an image, not the text. */
        void *item = list_item_create(view, 0, 0, 375, 48);
        widget_use_style(item, "s_listitem_black");
        widget_on(item, EVT_CLICK, action, (void *)(long)KEYBOARD);
        ui.edit = edit_create(item, "ufloat", 4, 40);
        peq_band *b = &ui.draft.bands[ui.band];
        snprintf(text, sizeof(text), ui.adjust == QUALITY ? "%.2f" : "%.0f", ui.adjust == QUALITY ? b->q : b->frequency);
        widget_set_text_utf8(ui.edit, text);
        widget_on(ui.edit, EVT_VALUE_CHANGED, typed, 0);
        widget_on(ui.edit, EVT_FOCUS, focused, 0);
        n = 1;
        if (ui.adjust == QUALITY) snprintf(value, sizeof(value), "0.05");
        else snprintf(value, sizeof(value), "%d Hz", steps[ui.step]);
        row_dim(view, n++, "增加", value, RAISE, 0);
        row_dim(view, n++, "减少", value, LOWER, 0);
        if (ui.adjust != QUALITY) row_dim(view, n++, "步长", value, STEP, 0);
    } else if (ui.screen == PICK) {
        for (int i = 0; i < picks[ui.pick - BALANCE].rows; ++i) {
            double v = picks[ui.pick - BALANCE].first + picks[ui.pick - BALANCE].step * i;
            if (ui.pick == BALANCE) /* L 12.0 dB to R 12.0 dB */
                snprintf(text, sizeof(text), i == 24 ? "居中" : "%s %.1f dB", v < 0 ? "L" : "R", __builtin_fabs(v));
            else if (ui.pick == PREAMP && !i) snprintf(text, sizeof(text), "自动 (%.1f dB)", headroom(&ui.draft));
            else snprintf(text, sizeof(text), "%+.1f dB", v);
            row(view, n++, text, i);
        }
    } else if (ui.screen == CONFIRM && ui.previous == DELETES) {
        snprintf(text, sizeof(text), "删除 %.100s？确认", deleting()); row(view, n++, text, YES);
        row(view, n++, "取消", CANCEL);
    } else if (ui.screen == CONFIRM) {
        row(view, n++, "替换现有预设？确认", YES);
        row(view, n++, "取消替换", CANCEL);
    } else if (ui.screen == STOCKS) {
        for (int i = 0; i < STOCK_N; ++i) row(view, n++, stock_names[i], i);
    } else if (ui.screen == SAVES) {
        for (int i = 0; i < 10; ++i) {
            snprintf(text, sizeof(text), "保存为 Manual %02d", i+1); row(view, n++, text, i);
        }
    } else {
        if (ui.screen == PRESETS) {
            row(view, n++, "内置预设", STOCK);
            row(view, n++, "从 SD /EQ 导入", IMPORT);
            row(view, n++, "保存编辑中的预设", SAVE);
            row(view, n++, "删除预设", DELETE);
        }
        list_files(ui.screen == IMPORTS ? PEQ_IMPORT : PEQ_SAVED, ui.screen == IMPORTS ? ".txt" : ".peq");
        int last = ui.count ? (ui.count - 1) / FILE_PAGE : 0;
        if (ui.file_page < 0) ui.file_page = 0;
        if (ui.file_page > last) { ui.file_page = last; selection = offset = 0; }
        if (ui.file_page) row(view, n++, "上一页", FILE_PREV);
        if (ui.file_page < last) row(view, n++, "下一页", FILE_NEXT);
        for (int i = 0, start = ui.file_page * FILE_PAGE; i < FILE_PAGE && start + i < ui.count; ++i)
            row(view, n++, ui.names[start + i], i);
    }
    widget_set_prop_int(view, "virtual_h", n * 48);
    /* ringnav keeps the selection above on a view whose row count it already knows. */
    if (ui.screen == PICK) widget_set_prop_int(view, "_ringnav_count", n);
    if (n * 48 < rows) { /* short lists: shrink so the list's white background never shows below the rows */
        widget_resize(list, 375, n * 48);
        widget_resize(view, 375, n * 48);
    }
    scroll_view_set_offset(view, 0, offset);
    ui.graph = 0;
    if (graph) {
        curve();
        ui.graph = widget_factory_create_widget(widget_factory(), "view", ui.page, 0, 48, 375, PEQ_GRAPH_H);
    }
    /* A message takes the title bar until the next action, so it never shrinks the list; on the
     * main screen the bar otherwise shows the tabs (peq_paint). */
    void *title = ui.screen == HOME ? bar(ui.page, 0, 375) : bar(ui.page, 8, 359);
    ui.bar = ui.screen == HOME ? title : 0;
    text[0] = 0;
    if (ui.status[0]) snprintf(text, sizeof(text), "%s", ui.status);
    else if (ui.screen == BAND) snprintf(text, sizeof(text), "频段 %d", ui.band + 1);
    else if (ui.screen == ADJUST) snprintf(text, sizeof(text), ui.adjust == QUALITY ? "频段 %d Q" : "频段 %d 频率 (Hz)", ui.band + 1);
    else if (ui.screen == PICK && ui.pick == GAIN) snprintf(text, sizeof(text), "频段 %d 增益", ui.band + 1);
    else if (ui.screen == PICK) snprintf(text, sizeof(text), ui.pick == PREAMP ? "前级增益" : "声道平衡");
    else if ((ui.screen == IMPORTS || ui.screen == PRESETS || ui.screen == DELETES) && ui.count > FILE_PAGE)
        snprintf(text, sizeof(text), "预设 %d/%d", ui.file_page + 1, (ui.count - 1) / FILE_PAGE + 1);
    else if (ui.screen != HOME) snprintf(text, sizeof(text), "预设");
    widget_set_text_utf8(title, text);
    widget_invalidate_force(ui.page, 0);
    return 7; /* RET_REMOVE */
}

/* 0xRRGGBB and an alpha to a color_t, whose bytes are r, g, b, a. Shared with visualizer.c. */
unsigned rgba(unsigned c, unsigned a) { return a << 24 | (c & 255) << 16 | (c & 0xff00) | c >> 16; }

/* UTF-8 s centred in x, y, w, h (draw_centred). Shared with visualizer.c. */
void caption(void *canvas, const char *s, int x, int y, int w, int h, unsigned px, unsigned color) {
    unsigned wide[16], n = 0;
    const unsigned char *p = (const unsigned char *)s;
    while (*p && n < sizeof wide / sizeof *wide) {
        unsigned c = *p++, extra = c < 0x80 ? 0 : c >= 0xc2 && c <= 0xdf ? 1 :
                                  c >= 0xe0 && c <= 0xef ? 2 : c >= 0xf0 && c <= 0xf4 ? 3 : 0;
        if (c >= 0x80 && !extra) c = 0xfffd;
        if (extra) {
            unsigned code = c & ((1u << (6 - extra)) - 1), i = 0;
            for (; i < extra && (p[i] & 0xc0) == 0x80; ++i) code = (code << 6) | (p[i] & 63);
            if (i == extra && code >= (extra == 1 ? 0x80u : extra == 2 ? 0x800u : 0x10000u) &&
                code <= 0x10ffff && (code < 0xd800 || code > 0xdfff)) {
                c = code;
                p += extra;
            } else c = 0xfffd;
        }
        wide[n++] = c;
    }
    int r[4] = {x, y, w, h};
    draw_centred(canvas, wide, n, r, px, color);
}

static void path(void *vg, const float *y) {
    vgcanvas_begin_path(vg);
    vgcanvas_move_to(vg, PEQ_GRAPH_X, y[0]);
    for (int x = 1; x < GRAPH_W; ++x) vgcanvas_line_to(vg, PEQ_GRAPH_X + x, y[x]);
}

/* The graph view (ringnav_paint, after stock): a faint grid at 0, +-6 and +-12 dB and 100 Hz, 1 kHz
 * and 10 kHz, labelled in grey; the response as a 2px line in the accent's light tone (white in
 * Stock), fading to clear toward 0 dB, the other channel's dimmer when the two differ; a ring on each
 * band in use, the edited band's larger and filled. Off: the line alone, grey. */
static void e3_paint(void *canvas), tabs_paint(void *canvas);
void peq_paint(void *w, void *canvas) {
    if (w && w == e3.view && P(canvas, CANVAS_LCD)) e3_paint(canvas);
    if (w && w == ui.bar && P(canvas, CANVAS_LCD)) tabs_paint(canvas);
    if (!w || w != ui.graph || !P(canvas, CANVAS_LCD)) return;
    static const int lines[] = {0, 6, -6, 12, -12}, ticks[] = {100, 1000, 10000};
    static const char *const labels[] = {"100", "1k", "10k"};
    unsigned fill = (unsigned)I(P(canvas, CANVAS_LCD), LCD_FILL_COLOR);
    unsigned tone = ui.draft.bypass ? 0x666666 : TONE;
    float zero = graph_y(0);
    for (int i = 0; i < 5; ++i) {
        canvas_set_fill_color(canvas, rgba(i ? 0x262626 : 0x4a4a4a, 255));
        canvas_fill_rect(canvas, PEQ_GRAPH_X, (int)graph_y(lines[i]), GRAPH_W, 1);
    }
    for (int i = 0; i < 3; ++i) {
        int x = (int)graph_x(ticks[i]);
        canvas_set_fill_color(canvas, rgba(0x262626, 255));
        canvas_fill_rect(canvas, x, PEQ_PLOT_TOP, 1, PEQ_PLOT_BOTTOM - PEQ_PLOT_TOP);
        caption(canvas, labels[i], x - 16, PEQ_PLOT_BOTTOM + 2, 32, PEQ_GRAPH_H - PEQ_PLOT_BOTTOM - 2, 12, CF_GREY);
    }
    canvas_set_fill_color(canvas, fill);
    void *vg = canvas_get_vgcanvas(canvas);
    if (!vg) return;
    int split = 0;
    for (int x = 0; x < GRAPH_W; ++x) split |= __builtin_fabsf(ui.curve[0][x] - ui.curve[1][x]) > 0.25f;
    vgcanvas_save(vg);
    vgcanvas_translate(vg, (float)I(canvas, CANVAS_X), (float)I(canvas, CANVAS_Y));
    if (!ui.draft.bypass) /* one path, two fills: each gradient is clear beyond 0 dB on its side */
        for (int side = 0; side < 2; ++side) {
            path(vg, ui.curve[0]);
            vgcanvas_line_to(vg, PEQ_GRAPH_X + GRAPH_W - 1, zero);
            vgcanvas_line_to(vg, PEQ_GRAPH_X, zero);
            vgcanvas_close_path(vg);
            vgcanvas_set_fill_linear_gradient(vg, 0, side ? PEQ_PLOT_BOTTOM : PEQ_PLOT_TOP, 0, zero,
                                              rgba(tone, 0x90), rgba(tone, 0));
            vgcanvas_fill(vg);
        }
    vgcanvas_set_line_width(vg, 2);
    for (int ch = split; ch >= 0; --ch) {
        path(vg, ui.curve[ch]);
        vgcanvas_set_stroke_color(vg, rgba(tone, ch ? 0x80 : 0xff));
        vgcanvas_stroke(vg);
    }
    for (int i = 0; i < ui.draft.count; ++i) {
        const peq_band *b = &ui.draft.bands[i];
        int edited = ui.screen == BAND && i == ui.band;
        if (!b->enabled && !edited) continue;
        vgcanvas_begin_path(vg);
        vgcanvas_arc(vg, graph_x(b->frequency), graph_y(b->gain), edited ? 5 : 3.5f, 0, 6.2831853f, 0);
        vgcanvas_set_fill_color(vg, rgba(edited ? tone : 0, 255));
        vgcanvas_fill(vg);
        vgcanvas_set_stroke_color(vg, rgba(edited ? 0xffffff : tone, 255));
        vgcanvas_stroke(vg);
    }
    vgcanvas_restore(vg);
}

static void e3_done(void);
/* Replaces the stock page's on_common_keyup: Return steps back one screen; on Simple it ends an
 * adjustment, else leaves. */
static int keyup(void *ctx, void *event) {
    if (I(event, EVENT_KEY) != KEY_RETURN) return 0;
    if (ui.tab == ADVANCED) action((void *)(long)BACK, ctx);
    else if (e3.adjusting) {
        e3_done();
        e3_show();
    } else navigator_back();
    return 11; /* RET_STOP */
}

static int e3_flush(void);
/* Leaving saves what Simple has unsaved and lets go of its Off comparison's held preamp. */
static int closed(void *ctx, void *event) {
    stop_timer(&e3.press);
    e3_flush();
    eq_state s;
    if (eq_load(&s) && s.hold && (s.hold = 0, eq_save(&s))) peq_stock_eq(1);
    memset(&e3, 0, sizeof e3);
    ui.bar = 0;
    stop_timer(&ui.timer);
    ui.page = 0;
    ui.view = 0;
    ui.graph = 0;
    free(ui.names);
    ui.names = 0;
    ui.capacity = ui.file_page = 0;
    return 0;
}

/* What plays, for both tabs: Simple's gains and switch, Advanced's switch. Boot instantiates the
 * filter only when the switch left PLAYSET/EQFLAG set; the flag then follows the mode
 * (peq_stock_eq), so a mode saved on with the flag clear is not playing. */
static void load(eq_state *s) {
    eq_load(s);
    e3.on = g_equalizer_flag && s->mode == EQ_3;
    for (int i = 0; i < 3; ++i) e3.gain[i] = s->eq3.bands[i].gain;
    ui.draft.bypass = !g_equalizer_flag || s->mode != EQ_PEQ;
}

/* The tab bar's tap, or Centre on it: the other tab, the wheel kept on the bar. Neither changes
 * what plays. */
static void tab_to(int tab) {
    if (tab == ui.tab) return;
    if (ui.tab == SIMPLE) {
        e3_done();
        if (e3.dirty) { e3_show(); return; } /* keep unsaved gains if saving failed */
    }
    eq_state s;
    load(&s);
    ui.tab = tab;
    ui.tabs = 1;
    ui.screen = HOME;
    ui.status[0] = 0;
    e3.focus = E3_TABS;
    e3.status = 0;
    if (!ui.timer) ui.timer = timer_add(render, 0, 1); /* not inside the press's own widget */
}

static int tab_touch(void *ctx, void *event) {
    int xy[2] = { I(event, EVENT_X), I(event, EVENT_Y) };
    widget_to_local(ui.page, xy);
    tab_to(xy[0] < 188 ? SIMPLE : ADVANCED);
    return 0;
}

/* The title label; the tabs' bar when the bar takes their taps. */
static void *bar(void *page, int x, int w) {
    void *title = label_create(page, x, 0, w, 48);
    widget_use_style(title, "s_label_white20c");
    widget_set_prop_int(title, "line_wrap", 1);
    if (w == 375) widget_on(title, EVT_POINTER_DOWN, tab_touch, 0);
    return title;
}

/* Audio settings' EQ row: opens on the tab it was last on (ui.tab, zeroed to Simple at boot).
 * Opening changes nothing. ponytail: kept in RAM, so a reboot goes back to Simple; saving it in
 * eq_state would keep it, add when asked. */
int peq_page_init(void *page, void *context) {
    if (!page) return 16;
    ui.page = page;
    ui.view = 0;
    ui.screen = HOME;
    ui.status[0] = 0;
    ui.tabs = 0;
    eq_state s;
    eq_load(&s);
    ui.draft = s.peq;
    /* ponytail: a moved Auto plays its old number until Apply; the player has no headroom(). */
    ui.dirty = follow_auto();
    load(&s);
    ui.named.bypass = ui.draft.bypass;
    if (memcmp(&ui.draft, &ui.named, sizeof(ui.named))) ui.applied[0] = 0; /* replaced since */
    memcpy(ui.name, ui.applied, sizeof(ui.name));
    e3.focus = E3_LOW;
    e3.h = widget_get_prop_int(page, "h", 290);
    widget_on(page, EVT_DESTROY, closed, 0);
    widget_on(page, EVT_KEY_UP, keyup, 0);
    widget_on(page, EVT_POINTER_MOVE_BEFORE, e3_touch, 0); /* a slider's drag */
    widget_on(page, EVT_POINTER_UP_BEFORE, e3_touch, 0);
    render(0);
    return 0;
}

/* The sliders' travel, under their names and over their values, which stay clear of the iPod
 * glass's lower corners. */
static int e3_top(void) { return E3_SLIDERS + 34; }
static int e3_bottom(void) { return e3.h - 60; }
static int e3_y(double gain) {
    return (e3_top() + e3_bottom()) / 2 - (int)(gain * (e3_bottom() - e3_top()) / (2 * E3_MAX));
}

static void e3_preset(peq_preset *p) {
    eq3_bands(p, e3.gain);
    p->preamp = headroom(p);
}

static void e3_show(void) {
    if (ui.bar && ui.tab == SIMPLE) widget_set_text_utf8(ui.bar, e3.status ? e3.status : "");
    if (ui.page) widget_invalidate_force(ui.page, 0);
}

/* The gains into the saved state, the mode as it is; the player told while it hears them. */
static int e3_flush(void) {
    stop_timer(&e3.save);
    if (!e3.dirty) return 1;
    eq_state s;
    eq_load(&s);
    e3_preset(&s.eq3);
    if (!eq_save(&s)) {
        e3.status = "保存失败，均衡器未变"; /* kept dirty: the next edit tries again */
        return 0;
    }
    e3.dirty = 0;
    if (s.mode == EQ_3 || s.hold) peq_stock_eq(1);
    return 1;
}

static int e3_tick(const void *unused) {
    e3.save = 0;
    e3_flush();
    e3_show();
    return 7; /* RET_REMOVE */
}

static void e3_set(int band, double gain) {
    gain = gain > E3_MAX ? E3_MAX : gain < -E3_MAX ? -E3_MAX : gain;
    if (gain == e3.gain[band]) return;
    e3.gain[band] = gain;
    e3.dirty = 1;
    if (!e3.save) e3.save = timer_add(e3_tick, 0, E3_SAVE_MS);
}

static void e3_done(void) {
    e3.adjusting = e3.dragging = 0;
    e3_flush();
}

/* On selects 3EQ (PEQ goes off); Off holds the 3EQ's preamp while the page is open, so comparing
 * against bypass is not suddenly louder. */
static void e3_switch(void) {
    eq_state s;
    eq_load(&s);
    e3_preset(&s.eq3);
    s.mode = e3.on ? EQ_OFF : EQ_3;
    s.hold = e3.on;
    if (!eq_commit(&s)) {
        e3.status = "切换失败，均衡器未变";
        return;
    }
    e3.on = !e3.on;
    e3.dirty = 0;
}

static void e3_activate(void) {
    e3.status = 0;
    if (e3.focus == E3_TABS) tab_to(ADVANCED);
    else if (e3.focus == E3_SWITCH) e3_switch();
    else if (e3.focus == E3_RESET) {
        for (int i = 0; i < 3; ++i) e3_set(i, 0);
        e3_flush();
    } else e3.adjusting = 1;
    e3_show();
}

static int e3_single(const void *unused) {
    e3.press = 0;
    if (ui.tab == ADVANCED) tab_to(SIMPLE);
    else e3_activate();
    return 7;
}

/* ringnav(), the page on top: Simple's wheel and Centre, and Advanced's tab bar, which the wheel
 * reaches past the list's first row. */
int eq_key(void *top, unsigned key) {
    static const unsigned release[EVENT_KEY / 4 + 1] = { [EVENT_KEY / 4] = KEY_CENTER };
    if (!ui.page || top != ui.page) return 0;
    if (ui.tab == ADVANCED) {
        if (ui.screen != HOME || !ui.view ||
            (!ui.tabs && (key != KEY_PREV || widget_get_prop_int(ui.view, "_ringnav_index", -1) > 0)))
            return 0; /* the list's */
        if (key == KEY_NEXT || key == KEY_PREV) {
            ui.tabs = key == KEY_PREV;
            widget_set_prop_int(ui.view, "_ringnav_index", ui.tabs ? -1 : 0);
            e3_show();
            return 1;
        }
    }
    if (key == KEY_CENTER && e3.adjusting) {
        e3_done();
        e3_show();
        return 1;
    }
    if (key == KEY_CENTER) {
        int press = center_press(&e3.press, &e3.press_at, e3_single, 0);
        if (press == 2 && g_backlight_status) on_wm_keyup_fun(0, (void *)release);
        return press;
    }
    if (key != KEY_NEXT && key != KEY_PREV) return 0;
    int dir = key == KEY_NEXT ? 1 : -1;
    if (e3.adjusting) e3_set(e3.focus - E3_LOW, e3.gain[e3.focus - E3_LOW] + 0.5 * dir);
    else e3.focus = e3.focus + dir < E3_TABS ? E3_TABS : e3.focus + dir > E3_RESET ? E3_RESET : e3.focus + dir;
    e3_show();
    return 1;
}

/* A touch: the switch or Reset on the press; a slider's gain follows the finger, in 0.5 dB steps,
 * until it lifts. */
static int e3_touch(void *ctx, void *event) {
    int type = I(event, EVENT_TYPE), xy[2] = { I(event, EVENT_X), I(event, EVENT_Y) };
    if (!e3.view || (type != EVT_POINTER_DOWN && !e3.dragging)) return 0;
    widget_to_local(e3.view, xy);
    if (type == EVT_POINTER_DOWN) {
        if (xy[1] < E3_BUTTONS) return 0;
        e3.status = 0;
        if (xy[1] < E3_SLIDERS) {
            e3.focus = xy[0] < 187 ? E3_SWITCH : E3_RESET;
            e3_activate();
            return 0;
        }
        e3.focus = E3_LOW + (xy[0] < 125 ? 0 : xy[0] < 250 ? 1 : 2);
        e3.adjusting = e3.dragging = 1;
    }
    if (type == EVT_POINTER_UP_BEFORE) e3_done();
    else {
        double half = (e3_bottom() - e3_top()) / 2.0, g = ((e3_top() + e3_bottom()) / 2 - xy[1]) * E3_MAX / half;
        e3_set(e3.focus - E3_LOW, (int)(g * 2 + (g < 0 ? -0.5 : 0.5)) / 2.0);
    }
    e3_show();
    return 0;
}

static void e3_box(void *canvas, int x, int y, int w, int h, unsigned rgb) {
    canvas_set_fill_color(canvas, rgba(rgb, 255));
    canvas_fill_rect(canvas, x, y, w, h);
}

/* The switch's bar and the gains' bars in the accent's light tone (white in Stock and Minimal), grey
 * while 3EQ is off; the selected control on a lighter card; the slider being adjusted with a white
 * handle and value, the others grey. */
static void e3_paint(void *canvas) {
    unsigned fill = (unsigned)I(P(canvas, CANVAS_LCD), LCD_FILL_COLOR), tone = e3.on ? TONE : 0x666666;
    int zero = e3_y(0);
    char text[16];
    for (int i = 0; i < 2; ++i) {
        int focus = e3.focus == (i ? E3_RESET : E3_SWITCH);
        e3_box(canvas, i ? 192 : 12, E3_BUTTONS, 171, 40, focus ? 0x333333 : 0x1a1a1a);
        if (!i) e3_box(canvas, 12, E3_BUTTONS + 36, 171, 4, tone);
        caption(canvas, i ? "重置" : e3.on ? "启用：打开" : "启用：关闭", i ? 192 : 12, E3_BUTTONS, 171, 40, 20, 0xffffffffu);
    }
    for (int i = 0; i < 3; ++i) {
        static const char *const names[] = { "低频", "中频", "高频" };
        int x = 62 + 125 * i, y = e3_y(e3.gain[i]), focus = e3.focus == E3_LOW + i;
        if (focus) e3_box(canvas, x - 56, E3_SLIDERS, 112, e3_bottom() + 36 - E3_SLIDERS, 0x1a1a1a);
        caption(canvas, names[i], x - 56, E3_SLIDERS, 112, 28, 20, 0xffffffffu);
        e3_box(canvas, x - 2, e3_top(), 4, e3_bottom() - e3_top(), 0x333333);
        e3_box(canvas, x - 3, y < zero ? y : zero, 6, y < zero ? zero - y : y - zero + 1, tone);
        e3_box(canvas, x - 20, y - 5, 40, 10, focus && e3.adjusting ? 0xffffff : 0x8a8a8a);
        snprintf(text, sizeof text, e3.gain[i] ? "%+.1f dB" : "0 dB", e3.gain[i]);
        caption(canvas, text, x - 56, e3_bottom() + 8, 112, 24, 18, focus && e3.adjusting ? 0xffffffffu : CF_GREY);
    }
    e3_box(canvas, 12, zero, 351, 1, 0x8a8a8a); /* 0 dB */
    canvas_set_fill_color(canvas, fill);
}

/* The tab bar, unless a message has it: the open tab white over an accent line, the other grey,
 * and while the wheel is on the bar, the other on the lighter card Centre would open. */
static void tabs_paint(void *canvas) {
    int simple = ui.tab == SIMPLE, focus = simple ? e3.focus == E3_TABS : ui.tabs;
    if (simple ? e3.status != 0 : ui.status[0] != 0) return;
    unsigned fill = (unsigned)I(P(canvas, CANVAS_LCD), LCD_FILL_COLOR);
    for (int i = 0; i < 2; ++i) {
        int x = i ? 191 : 8, shown = i == !simple;
        if (focus && !shown) e3_box(canvas, x, 4, 176, 40, 0x333333);
        caption(canvas, i ? "高级" : "简单", x, 4, 176, 40, 20, shown ? 0xffffffffu : CF_GREY);
        if (shown) e3_box(canvas, x + 40, 42, 96, 3, TONE);
    }
    canvas_set_fill_color(canvas, fill);
}
