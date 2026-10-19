# 本地简体中文版

这是基于本仓库 Q2 Pod 1.3.8、用于 Shanling Q2 的个人自用固定简体中文版，不是山灵官方固件，也不是上游发布的正式中文版本。

## 翻译范围

新增菜单、设置选项和本地提示固定为简体中文，包括主菜单的音乐/Cover Flow/设置入口、评价、播放队列、随机/重复播放、均衡器、交叉淡化、转轮灵敏度、流媒体设置、Spotify、网络电台、书籍/视频与照片。

本轮以 Apple 的 [iPod classic 2009 简体中文使用手册](https://cdsassets.apple.com/live/6GJYWVAV/user/locale/zh-cn/ma1195_ipod_classic_160gb_user_guide.pdf) 为术语参考：主菜单入口使用“音乐”，保留“Cover Flow”，使用“表演者”“有声读物”“评价”“重复播放”和“打开/关闭”。Q2 独有功能采用相近风格，不伪称为原版 iPod 功能；评价仍使用本项目的爱心图标，不改成星星。

设置选项同时统一为“主菜单：分栏/全屏”“低功耗模式：打开/关闭”“主题：简约/经典”“电池显示：图标与百分比”等。未重写原厂全部中文语言资源，因此原厂页面里可能仍有与新增菜单不同的历史用词。

原厂页面继续使用原厂语言机制。建议在设备的系统设置中选择简体中文；切换到英文或其他语言时，本次新增文案仍然显示中文。品牌、音频单位、歌曲/艺术家/专辑/文件名、外部服务回复及部分底层错误保持原文；没有修改存储卡目录名、配置键或协议字段。

自绘 caption 原先仅按 ASCII 字节绘制，现已支持 UTF-8 转 Unicode，并保留 16 码点长度限制及非法编码替换。自绘音量文字也改为中文码点。

## 上游基线

汉化提交以 rebase 方式重放到 upstream 正式 `1.3.8` 标签（`3744dac1e1aa2958d0870b8048537cdf021050d5`），不包含标签之后 main 的未发布改动。保留上游新增章节导航、播放列表编辑、左右声道互换、均衡器预设及队列顺序等逻辑，并补译相关界面。

## 构建

需要通过项目校验的原厂 V1.32 ZIP，SHA-256 为：

```text
154c17822d09be001be35c03d2d3488424dee195221790bd70864480d55b0f00
```

本机补齐的工具放在忽略版本管理的 `work/toolchain/`，不会替换系统工具。使用 Python 3.12 虚拟环境（既有 MIPS 测试有 Python 3.12 f-string 语法）：

```bash
UV_CACHE_DIR="$PWD/.cache/uv" uv venv .venv
UV_CACHE_DIR="$PWD/.cache/uv" uv pip install --python .venv/bin/python -r requirements.txt
export PATH="$PWD/work/toolchain/bin:$PATH"
.venv/bin/python tools/build.py '/home/ericwyn/Downloads/Q2 Firmware V1.32.zip' \
  --out build-cn-138 --ipod
```

输出目录必须不存在；重复构建请换一个新目录。

当前本地工具链为 LLVM 14、SquashFS 4.6.1（含 LZO 支持）与系统 ImageMagick。不要用系统 SquashFS 4.5 代替项目要求的新版本。LZO 是原厂文件系统的解压格式。

不带 `--dev` 的固件没有新增 q2remote 远程调试服务，适合日常使用。带 `--dev` 的开发版包含该调试服务，并使用独立版本标记，不建议作为首选日常安装包。

## 验证与限制

字体覆盖检查数量会随新增文案变化，使用下述中文专项检查验证实际打包字体；中文均衡器预览没有发现明显裁切。截图由主机测试绘制，不是设备截图，仍需实机确认各菜单、滚动标题和蓝牙/流媒体流程。

可运行的中文专项检查与主要测试：

```bash
.venv/bin/python test/chinese.py build-cn-138
.venv/bin/python test/peq.py
.venv/bin/python test/coverflow.py
.venv/bin/python test/playback.py build-cn-138
.venv/bin/python test/patch.py build-cn-138
.venv/bin/python test/build.py --build build-cn-138
```

本机 `test/build.py --build ...` 的完整原始运行存在一个已复现的上游/环境差异：60px `scrsaver_playcover.png` 的角像素透明度由本机 ImageMagick 算出为 2/255，而测试要求严格为 0。未修改的 HEAD 中同一个图片缩放函数也产生 2/255。没有为了过测试修改这个断言或不相关的图像生成代码；单独在内存中报告该检查并继续运行后，其余打包资源断言通过。完整测试不能宣称全部通过。

## 安装和回滚

先备份卡内资料，设备充足电。将安装包中的 `update.tar` 放入存储卡根目录，安全弹出后通过系统设置的 TF 卡升级安装。不要在刷机期间断电或拔卡。

1.3.8 日常版标记为 `V138I`（关于页面版本 1.3.8），不同于此前 1.3.0 中文日常版的 `V130I`，因此从旧版可直接升级日常包，无需经过开发版。如果已经安装上游英文 1.3.8（同为 `V138I`），先刷入本次版本标记不同的中文开发包，再刷中文日常包即可。开发包包含新增的密钥保护远程调试服务，不建议长期停留。不要手工修改升级包或校验值。

需要回滚时，按山灵官方说明使用 Q2 官方 V1.32 固件从 TF 卡升级。本地中文修改不影响这一恢复途径。卡内文件通常不因正常升级而格式化，但设置、评分、续播记录等内部数据不保证被原厂固件保留或识别。


## 网络更新的重要提示

上游新增的网络更新仍从 DiamondBond/q2-pod 下载原版英文固件，不会获取本地中文 Fork。为避免误会，此版行标题明确显示“上游英文固件更新”。点击并完成安装会失去本地汉化；如需保留中文，请使用本 Fork 构建的固件通过 TF 卡升级。没有悄悄将下载地址改为缺少 Release 产物的 Fork，也没有弱化 TLS、版本或写入检查。
