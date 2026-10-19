/* Network Update (docs/internals.md#network-update): System Update's second row fetches the latest
 * Q2 Pod release from GitHub to the card's update.tar, then hands over to stock's TF card update. */
#include "peq.h"

/* The tag in a releases/latest redirect, ".../releases/tag/1.3.4": what follows the last '/'. */
const char *netupdate_tag(const char *location) {
    const char *slash = location ? strrchr(location, '/') : 0;
    return slash && strstr(location, "/releases/tag/") && slash[1] ? slash + 1 : 0;
}

#ifndef PEQ_HOST
#include "offsets.inc"
#include "stock.h"

#define NU_REPO "https://github.com/DiamondBond/q2-pod/releases/latest"
#if IPOD /* tools/release.py ASSETS */
#define NU_ASSET "update.tar"
#else
#define NU_ASSET "update-legacy.tar"
#endif
#define NU_FILE "/mnt/mmc/update.tar"
#define NU_CA "/etc/scrobble-ca.pem" /* scrobble.c's; github.com's and its asset host's roots */

enum { NU_IDLE, NU_RUNNING, NU_DONE, NU_LATEST, NU_NET, NU_MISSING, NU_CARD, NU_CANCELLED };

static struct {
    unsigned long thread;
    volatile int state, cancel, percent;
    void *label; /* the row's label while System Update is open, for the percentage */
} nu __attribute__((section(".scratch")));

extern int stock_fwupdate_trampoline(void *win, void *ctx);

static unsigned save(const void *data, unsigned size, unsigned n, void *file) {
    return fwrite(data, size, n, file) * size;
}

static int progress(void *unused, long long total, long long now, long long c, long long d) {
    if (total >= 1024) nu.percent = (unsigned)(now >> 10) * 100 / (unsigned)(total >> 10); /* KB: 32-bit math */
    return nu.cancel;
}

/* One transfer: the latest tag (url without asset, headers only, no redirect) or the asset to file. */
static int fetch(const char *url, void *file, char *tag, unsigned n, long *code) {
    void *c = curl_easy_init();
    int err = 1;
    char *location = 0;
    if (!c) return 1;
    curl_easy_setopt(c, 10002, url);          /* CURLOPT_URL */
    curl_easy_setopt(c, 10018, "Q2Pod");      /* USERAGENT */
    curl_easy_setopt(c, 99, 1L);              /* NOSIGNAL: off the UI thread */
    curl_easy_setopt(c, 78, 15L);             /* CONNECTTIMEOUT */
    curl_easy_setopt(c, 10065, NU_CA);        /* CAINFO */
    curl_easy_setopt(c, 64, 1L);              /* SSL_VERIFYPEER */
    curl_easy_setopt(c, 81, 2L);              /* SSL_VERIFYHOST */
    if (file) {
        curl_easy_setopt(c, 52, 1L);          /* FOLLOWLOCATION: to the asset host */
        curl_easy_setopt(c, 20011, save);     /* WRITEFUNCTION */
        curl_easy_setopt(c, 10001, file);     /* WRITEDATA */
        curl_easy_setopt(c, 19, 1024L);       /* LOW_SPEED_LIMIT: a stalled transfer ends */
        curl_easy_setopt(c, 20, 30L);         /* LOW_SPEED_TIME */
        curl_easy_setopt(c, 20219, progress); /* XFERINFOFUNCTION */
        curl_easy_setopt(c, 43, 0L);          /* NOPROGRESS off, for it */
    } else {
        curl_easy_setopt(c, 44, 1L);          /* NOBODY */
        curl_easy_setopt(c, 13, 30L);         /* TIMEOUT */
    }
    /* curl itself fails a body shorter than its Content-Length (PARTIAL_FILE). */
    err = curl_easy_perform(c) || curl_easy_getinfo(c, 0x200002, code); /* RESPONSE_CODE */
    if (!err && tag && !curl_easy_getinfo(c, 0x10001f, &location)) {    /* REDIRECT_URL */
        const char *t = netupdate_tag(location);
        tk_snprintf(tag, n, "%s", t ? t : "");
    }
    curl_easy_cleanup(c);
    return err;
}

static void *worker(void *unused) {
    char tag[64] = "";
    long code = 0;
    if (fetch(NU_REPO, 0, tag, sizeof tag, &code) || code != 302 || !*tag) {
        nu.state = NU_NET;
        return 0;
    }
    /* Tags are single-digit x.y.z, so strcmp orders them: nothing older than this build, which
     * stock's updater would flash. Q2POD_TAG is empty in --dev builds, which take any tag. */
    if (*Q2POD_TAG && strcmp(tag, Q2POD_TAG) <= 0) {
        nu.state = NU_LATEST;
        return 0;
    }
    /* The old update.tar stays until the new one is whole. ponytail: needs the tar's ~55 MB free
     * beside it; TLS and curl's length check are the only integrity checks before stock's own MD5s. */
    if (nu.cancel) { /* pressed while checking, or the page was left */
        nu.state = NU_CANCELLED;
        return 0;
    }
    void *f = fopen(NU_FILE ".part", "wb");
    int err = !f, state = NU_CARD;
    if (f) {
        err = fetch(NU_REPO "/download/" NU_ASSET, f, 0, 0, &code);
        state = nu.cancel ? NU_CANCELLED : err ? (ferror(f) ? NU_CARD : NU_NET) : code != 200 ? NU_MISSING : NU_CARD;
        err = err || code != 200 || fflush(f) || fsync(fileno(f)) || ferror(f);
        err = fclose(f) || err;
    }
    if (!err && !nu.cancel && !rename(NU_FILE ".part", NU_FILE)) { /* a late cancel still cancels */
        void *dir = fopen("/mnt/mmc", "r"); /* the rename's directory entry, before stock flashes */
        if (dir) fsync(fileno(dir)), fclose(dir);
        state = NU_DONE;
    } else
        unlink(NU_FILE ".part");
    nu.state = state;
    return 0;
}

static void show(const char *text) {
    if (nu.label) widget_set_text_utf8(nu.label, text);
}

static int wait_done(const void *unused) {
    static const char *const said[] = {
        [NU_LATEST] = "已是最新上游版本", [NU_NET] = "无法连接 GitHub",
        [NU_MISSING] = "此版本暂无更新", [NU_CARD] = "无法写入存储卡",
        [NU_CANCELLED] = "已取消下载",
    };
    char s[32];
    if (nu.state == NU_RUNNING) {
        reset_poweroptions_timer(1, 1, 0); /* standby, auto power-off; not the screen's */
        tk_snprintf(s, sizeof s, "正在下载 %d%%", nu.percent);
        show(nu.cancel ? "正在取消…" : nu.percent ? s : "正在检查…");
        return 8; /* RET_REPEAT */
    }
    pthread_join(nu.thread, 0);
    int state = nu.state;
    nu.state = NU_IDLE;
    if (nu.label) show("上游英文固件更新");
    if (state != NU_DONE)
        toast(said[state]);
    else if (nu.label)
        fwupdate_tf(); /* stock's TF card update: its battery check and confirm */
    else
        toast("上游英文固件已下载，请使用 TF 卡升级");
    return 0;
}

/* A second press cancels: the transfer ends at its next progress call. */
static int online_click(void *ctx, void *event) {
    if (nu.state == NU_RUNNING) {
        nu.cancel = 1;
        show("正在取消…");
    } else if (nu.state != NU_IDLE) {
        /* ended, not yet joined by wait_done */
    } else if (get_wifisignal() <= 0) {
        toast("请先连接 Wi-Fi");
    } else {
        nu.cancel = nu.percent = 0;
        nu.state = NU_RUNNING;
        if (pthread_create(&nu.thread, 0, worker, 0)) {
            nu.state = NU_IDLE;
            toast("无法开始下载");
        } else {
            show("正在检查…");
            timer_add(wait_done, (void *)0, 500);
        }
    }
    return 0;
}

/* Leaving the page cancels: the card may be unmounted or shared over USB next. */
static int label_gone(void *ctx, void *event) {
    if (nu.label == ctx) nu.label = 0, nu.cancel = 1;
    return 0;
}

/* systemset_fwupdate_page_init: stock's two rows (0x4c3140), each button named by its index, which
 * stock's click (0x4c2fac) reads with atoi: 0 TF card update, 1 Shanling's server. Renamed "2", the
 * second does nothing in stock's click and ours runs. */
int ringnav_fwupdate(void *win, void *ctx) {
    int result = stock_fwupdate_trampoline(win, ctx);
    void *view = win ? widget_lookup(win, "scroll_view_fwupdate", 1) : (void *)0;
    void *button = view && widget_count_children(view) == 2 ? widget_get_child(widget_get_child(view, 1), 0) : 0;
    if (button) {
        widget_set_name(button, "2");
        widget_on(button, EVT_CLICK, online_click, 0);
        nu.label = widget_get_child(button, 0);
        widget_on(nu.label, EVT_DESTROY, label_gone, nu.label);
        show(nu.state == NU_RUNNING ? "正在下载…" : "上游英文固件更新");
    }
    return result;
}
#endif
