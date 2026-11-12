# 本地简体中文版

这是基于本仓库 Q2 Pod 1.7.6、用于 Shanling Q2 的个人自用固定简体中文版，不是山灵官方固件，也不是上游发布的正式中文版本。

当前开发分支为 `update-1.7.6`，基线是上游标签 `1.7.6`（`37a37bda2d95a65f7f6ac59231c3999ed8ee7753`）。`1.8.0` 当时是预发布版本，没有纳入本次基线。

## 翻译范围

新增菜单、设置选项和本地提示固定为简体中文，包括主菜单的音乐/Cover Flow/设置入口、评价、播放队列、随机/重复播放、均衡器、交叉淡化、转轮灵敏度、流媒体设置、Spotify、网络电台、书籍/视频与照片。

本轮以 Apple 的 [iPod classic 2009 简体中文使用手册](https://cdsassets.apple.com/live/6GJYWVAV/user/locale/zh-cn/ma1195_ipod_classic_160gb_user_guide.pdf) 为术语参考：主菜单入口使用“音乐”，保留“Cover Flow”，使用“表演者”“有声读物”“评价”“重复播放”和“打开/关闭”。Q2 独有功能采用相近风格，不伪称为原版 iPod 功能；评价仍使用本项目的爱心图标，不改成星星。

1.7.6 新增的主题、外观、设置分组、EQ per Output、播放列表选择模式、应用商店、睡眠定时和 USB 弹出提示也已补齐中文。

设置选项统一为“主菜单：分栏/全屏/旋转封面”“低功耗模式：打开/关闭”“主题：简约/经典”“电池显示：图标与百分比”“按输出独立 EQ”“标题显示时间”等。未重写原厂全部中文语言资源，因此原厂页面里可能仍有与新增菜单不同的历史用词。

原厂页面继续使用原厂语言机制。建议在设备的系统设置中选择简体中文；切换到英文或其他语言时，本次新增文案仍然显示中文。品牌、音频单位、歌曲/艺术家/专辑/文件名、外部服务回复及部分底层错误保持原文；没有修改存储卡目录名、配置键或协议字段。`Podcasts`、`Audiobooks`、`Photos`、`Books`、`Videos` 仍是卡内真实目录名，界面另用中文显示标签。

自绘 caption 原先仅按 ASCII 字节绘制，现已支持 UTF-8 转 Unicode，并保留 32 码点长度限制及非法编码替换。自绘音量文字也改为中文码点。

## 上游基线

- `1.4.8` 汉化保留在独立的 `update-1.4.8` 分支。
- `1.5.5` 汉化和百度/酷狗修复保留在 `update-1.5.5.fix` 分支。
- 上一版迁移为 `update-1.6.3`，基线是上游 `1.6.3`。
- 当前 `update-1.7.6` 基于上游 `1.7.6`，不修改 Fork 的 `main`，并保留原厂百度网盘入口。

## 1.7.6 本地修复

- 百度网盘：恢复原厂 `baidu_pthread_playurl` 的 10ms 轮询，不再生成会永久阻塞百度线程的 `ringnav_baidu_poll`；Tidal 的信号量按需唤醒保持不变。
- 播放中歌曲的歌词发现：先尝试 LRCLIB；无同步歌词、连接失败、忙碌重试用尽或写入失败时，恢复原厂酷狗搜索，之后仍允许原厂流程尝试网易云。酷狗可能匹配到不同版本或其他歌曲，保存前请预览。
- 批量“下载专辑歌词”仍只使用 LRCLIB；酷狗回退只作用于原厂单曲“封面/歌词发现”。
- 保留 1.6.3 已补齐的夜间模式、应用商店、显示设置、Crossfade、Resume Songs、书籍、游戏、照片、视频和播放重启提示。
- 对 1.7.6 的主题系统、设置分组、EQ per Output、播放列表和 USB 提示重新迁移汉化，没有整文件回退上游新功能。
- 为当前 GCC `-Werror` 保留了无行为变化的可移植性修正：`peq.h` 检查 `write()` 返回值，`netupdate.c` 用 `run_best_effort()` 显式忽略 `system()` 返回值。

## 构建

需要通过项目校验的原厂 V1.32 ZIP，SHA-256 为：

```text
154c17822d09be001be35c03d2d3488424dee195221790bd70864480d55b0f00
```

本机补齐的工具放在忽略版本管理的 `work/toolchain/`，不会替换系统工具。1.7.6 当前使用：

- LLVM/Clang/Lld 21
- SquashFS 4.6.1
- ImageMagick 7
- Python 3.14 venv（构建与普通测试）
- PIE Python 3.12（`test/fw` 的固定地址主机测试）

```bash
export PATH="$PWD/work/toolchain/bin:$PATH"
.venv/bin/python tools/build.py '/tmp/Q2-Firmware-V1.32.zip' \
  --out build-cn-176-dev-5 --dev
```

1.7.6 的构建命令不再使用 `--ipod`。输出目录必须不存在；重复构建请换一个新目录。

## 验证与限制

当前最终开发构建：

```text
版本：V00Li
dev：21
update.tar SHA-256：18ee7b1911dd7a3688b9b417627d072a198db1f836391bf03bf70dcdbeffd77c
source_sha256：5c0dcda98cfa90bd11aed34c8086fdc05b0a81e52f840f1ed05fb05f38844e42
```

`manifest.json` 的 `source_sha256` 与当前源码一致。主机端单元测试通过 1211 项，中文、播放、PEQ、Cover Flow 和主要固件场景已做回归；`V00Li` 已完成 Q2 实机安装与主要界面/播放验收。

本机 `test/build.py --build ...` 仍有一个已复现的上游/环境差异：`scrsaver_playcover.png` 的角像素透明度由当前 ImageMagick 算出为 2/255，而测试要求严格为 0。没有修改断言或不相关的图像生成代码。

可运行的中文专项检查与主要测试：

```bash
.venv/bin/python test/chinese.py build-cn-176-dev-5
.venv/bin/python test/peq.py
.venv/bin/python test/coverflow.py
.venv/bin/python test/playback.py build-cn-176-dev-5
.venv/bin/python test/patch.py build-cn-176-dev-5
.venv/bin/python test/build.py --build build-cn-176-dev-5
```

## 安装和回滚

先备份卡内资料，设备充足电。将安装包中的 `update.tar` 放入存储卡根目录，安全弹出后通过系统设置的 TF 卡升级安装。不要在刷机期间断电或拔卡。

本次 `V00Li` 使用独立 dev 标记，可直接从 `V155I`、`V00Gi` 或其他旧包通过 TF 卡升级，不需要先降级或手工修改安装包、版本字段及校验值。实际标记和 dev 编号以构建输出的 `manifest.json` 和安装包内的 `firmware_v20.info` 为准。

需要回滚时，按山灵官方说明使用 Q2 官方 V1.32 固件从 TF 卡升级。本地中文修改不影响这一恢复途径。卡内文件通常不因正常升级而格式化，但设置、评分、续播记录等内部数据不保证被原厂固件保留或识别。


## 网络更新的重要提示

上游新增的网络更新仍从 DiamondBond/q2-pod 下载原版英文固件，不会获取本地中文 Fork。为避免误会，此版行标题明确显示“上游英文固件更新”。点击并完成安装会失去本地汉化；如需保留中文，请使用本 Fork 构建的固件通过 TF 卡升级。没有悄悄将下载地址改为缺少 Release 产物的 Fork，也没有弱化 TLS、版本或写入检查。


## 中文界面与键盘

保留上游正确的控件、资源及输入处理，但不执行“将所有原厂中文占位标签改成英文、移除中文输入入口”的本地化策略。中文键盘保留；新增固定文字继续使用 iPod classic 简体中文术语。自绘文字保留上游 32 码点容量并支持 UTF-8，游戏标题/提示与均衡器共用此路径。
