# Gold Monitor · 金价悬浮窗

Windows 桌面小工具，以小型置顶悬浮窗显示最新黄金报价，也可收起为靠近任务栏的价格条。主报价为 **美元／金衡盎司（USD / troy oz）**，同时显示按美元兑人民币汇率折算的 **人民币／克（CNY / g）**。

## 下载与启动

**[下载 Windows 版 GoldMonitor.exe](https://github.com/JhuoW/GoldMonitor/releases/latest/download/GoldMonitor.exe)** · [查看发布版本](https://github.com/JhuoW/GoldMonitor/releases/latest)

下载后双击 `GoldMonitor.exe` 即可启动。成品是独立程序，不需要安装 Python，也不需要 API Key。

通过 Git 克隆或 GitHub 的 **Download ZIP** 获取的是源码，不包含已编译的 EXE。请下载上面的成品，或按下方“构建 EXE”说明运行 `build.ps1`。本地构建后双击 `dist\GoldMonitor.exe`，也可使用项目中的 `launch.vbs` 启动已打包的 EXE。

## 使用

- 拖动悬浮窗移动位置；双击标题切换精简模式。
- 默认保持在其他窗口上方，`Ctrl+P` 切换置顶状态。
- `Ctrl+M` 切换精简模式，减少桌面占用。
- 点击标题栏的 `－` 或按 `Ctrl+H`，收起为按文字宽度自动收紧的任务栏价格条；单击价格条恢复完整窗口。
- `F5` 手动刷新，`Alt+F4` 退出。
- 完整窗口和价格条均可右键打开菜单，切换数据源、调整刷新间隔、查看数据来源及退出；窗口置顶、精简模式与不透明度在完整窗口菜单中调整。
- 默认使用 **Swissquote XAU/USD** 买卖报价中间价，每 **1 秒**查询一次；菜单中保留 **Gold API** 数据源，其最短查询间隔为 **30 秒**。

“每秒刷新”表示客户端每秒查询真实报价，并不保证价格每秒发生变化。主报价使用 Swissquote 的买价与卖价平均值；市场休市或上游尚未更新时，价格及来源时间可以保持不变。人民币报价使用每日更新的参考汇率，不是逐笔外汇行情。

手动刷新遵守当前数据源的最短请求间隔。快捷键在悬浮窗获得焦点时生效。

## 任务栏价格条

价格条使用无边框透明背景，文字颜色跟随 Windows 任务栏的浅色／深色主题。宽度按实际文字计算，保留少量边距；涨跌箭头预留位置，避免数字跳动时整个条左右晃动。空白处同样可以点击和拖动。

右键菜单在价格条上方或相邻工作区打开，自动避开任务栏和屏幕边缘。刷新频率、行情来源在同一面板内切换，按返回或 Esc 回到上一级；在首页按 Esc 或点击外部关闭。菜单打开期间金价继续刷新，价格条保持位置稳定。

价格条是独立小窗口，优先贴合任务栏经过检测的空闲区域。拖动价格条可调整位置；定位时会避开已识别的任务栏按钮、托盘和其他控件。若找不到经过确认的空隙，或任务栏位于屏幕左、右侧，会放在任务栏上方或相邻位置。

开启任务栏自动隐藏时，价格条会随任务栏隐藏；当前屏幕进入全屏应用时也会暂时隐藏。它不会替换 Windows 原生天气组件，也不会修改注册表或系统 Shell。

## 设置与常见问题

数据源、完整窗口和价格条的位置、收起状态、置顶状态、精简模式、不透明度及刷新间隔会自动保存在程序所在目录的 `.state` 文件夹中。EXE 默认位置对应 `dist\.state`；源码运行对应项目根目录的 `.state`。程序不会设置开机自启。

- **行情未更新：** 在右键菜单选择“数据来源与状态”，查看金价和汇率的来源时间。网络或服务异常时，等待下一次自动刷新，或稍后按 `F5` 重试。
- **任务栏价格条暂时不见了：** 先退出全屏应用，或将鼠标移到任务栏边缘显示自动隐藏的任务栏；价格条会重新出现。
- **启动后没有找到窗口：** 程序只运行一个实例，再次启动会尝试激活已有窗口。需要重置位置时，先退出程序，再移走 `.state\settings.json` 后重开。
- **需要排查错误：** 运行日志位于 `.state\gold-monitor.log`。源码版本也可在终端执行 `python gold_monitor.pyw` 查看启动错误。

## 报价和换算

```text
人民币／克 = 美元／金衡盎司 × USD/CNY ÷ 31.1034768
```

1 金衡盎司 = 31.1034768 克。人民币数值是国际黄金报价的换算值，不含品牌溢价、加工费或零售加价。

默认金价来源：Swissquote 的 XAU/USD 买卖报价，显示值为 `(bid + ask) ÷ 2`。报价属于该经纪商的黄金美元报价；产品及计量信息见 [Swissquote Forex Product Guide](https://library.swissquote.com/cdn/2958/ql/forex-product-guide-ltd-en)。

可选金价来源：[Gold API](https://gold-api.com/docs)，接口 `https://api.gold-api.com/price/XAU`，最短查询间隔 30 秒。

汇率来源：[Rates By Exchange Rate API](https://www.exchangerate-api.com)，接口 `https://open.er-api.com/v6/latest/USD`；[开放接口说明](https://www.exchangerate-api.com/docs/free)说明其汇率每日更新。

## 从源码运行

需要 Windows、Python 3.10 或更新版本，并包含 Tkinter。运行时仅使用 Python 标准库。

```powershell
python gold_monitor.pyw
```

使用 `pythonw.exe gold_monitor.pyw` 可启动无控制台窗口的版本。联网后自动取价。

## 构建 EXE

在当前项目目录打开 PowerShell：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
```

脚本将 PyInstaller 安装到项目内的 `.build-tools` 目录，不修改系统 Python 环境；随后生成 `dist\GoldMonitor.exe`。工具已经下载过时，可跳过安装：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1 -SkipInstall
```

如需指定 Python，使用 `-Python 'C:\path\to\python.exe'`。构建方式使用 PyInstaller 的 [Windows 单文件、无控制台选项](https://pyinstaller.org/en/stable/usage.html)。

构建脚本不会结束已运行的程序。如果原 EXE 正在运行或被占用，脚本会停止覆盖；可自行退出原程序后重试，或生成独立的新文件：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1 -SkipInstall -OutputName GoldMonitor-Updated
```

此时输出为 `dist\GoldMonitor-Updated.exe`；`launch.vbs` 仍指向原来的 `GoldMonitor.exe`。

应用图标源文件在 `assets\gold.svg`；运行 `python assets\make_icon.py` 可重新生成多尺寸 Windows 图标 `assets\gold.ico`。
