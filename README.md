# 沪深主板选股筛选器

按「均线二次金叉 + MACD 金叉 + 近期涨停」的组合条件，从沪深主板全市场筛选个股。

提供 **PC 命令行版（Python）** 与 **Android 后台版（Kotlin）** 两套独立实现，筛选逻辑一致，
可指定任意历史基准日期回溯当时的信号。

> 数据源：新浪财经公开行情接口。本项目仅做数据筛选，**不构成任何投资建议**。

---

## 筛选条件

| # | 条件 | 说明 |
|---|------|------|
| 1 | 市场 | 沪深主板 —— 沪市 `600 / 601 / 603 / 605`，深市 `000 / 001 / 002` |
| 2 | 股价 | 3 ~ 30 元（按基准日收盘价判定） |
| 3 | 流通市值 | 30 亿 ~ 200 亿 |
| 4 | 均线信号 | 最近 20 个交易日内 MA5 **第二次**上穿 MA10，且最新一天刚好金叉 |
| 5 | MACD | 最新一天 DIF 上穿 DEA（EMA6 / EMA13，DEA = EMA5） |
| 6 | 涨停 | 最近 20 个交易日内出现过涨停（主板 10% 口径，按涨幅 ≥ 9.8% 判定） |
| 7 | 排除 | 剔除 ST、\*ST 等风险警示股 |

**关于「第二次金叉」**：在最近 20 个交易日的窗口内，MA5 上穿 MA10 恰好累计发生 2 次，
且第 2 次就落在最新一个交易日 —— 即刚完成二次金叉，而不是已经金叉了一段时间。

条件 4 与 5 为「与」关系，两者需同时满足。

---

## 项目结构

```
.
├── pc/                         # PC 命令行版（Python，零第三方依赖）
│   ├── screen.py               # 主程序
│   ├── build_exe.py            # 用 PyInstaller 打包为单文件 exe
│   └── sample_output.csv       # 示例输出
│
├── android/                    # Android 版（Kotlin）
│   ├── app/src/main/
│   │   ├── java/com/example/stockscreen/
│   │   │   ├── MainActivity.kt     # WebView 宿主 + JS 桥
│   │   │   ├── ScreenService.kt    # 前台 Service，后台筛选 + 通知
│   │   │   ├── StockScreen.kt      # 筛选逻辑（含指标计算）
│   │   │   └── HttpUtil.kt         # HTTP 请求封装
│   │   ├── assets/index.html       # 界面（原生 HTML/CSS/JS）
│   │   └── AndroidManifest.xml
│   ├── build.gradle.kts
│   ├── settings.gradle.kts
│   └── gradle.properties
│
├── build_android.bat           # 一键构建 Debug APK
└── .gitignore
```

---

## PC 版

**环境**：Python 3.8+，**无需安装任何第三方库**。

```bash
cd pc
python screen.py
```

运行后会提示输入基准日期：

```
请输入筛选基准日期（格式 YYYY-MM-DD，例如 2026-08-28）：
```

程序取该日或之前最近一个交易日的收盘数据作为基准，随后：

1. 分页拉取沪深 A 股全市场列表（约 5500 只）；
2. 按流通市值 + 板块 + ST 做初筛；
3. 逐只拉取日 K 线并计算信号（每次请求间隔 0.3 秒，避免被限流）；
4. 结果写入桌面，文件名形如 `股票筛选结果_20260828_143012.csv`。

**输出格式**（UTF-8 BOM，Excel 双击打开不乱码）：

| 代码 | 名称 | 基准日期 | 收盘价 | 流通市值(亿) |
|------|------|----------|--------|--------------|
| 600698 | 湖南天雁 | 2026-08-28 | 7.34 | 60.96 |

### 打包为 exe

双击项目根目录的 **`build_pc.bat`**（推荐），或手动执行：

```bash
cd pc
pip install pyinstaller
python build_exe.py
```

单文件 exe 输出到桌面。

> 注意：exe 文件名由 `pc/build_exe.py` 生成，形如 `股票筛选_20260923_234512.exe`。

### 可调参数

集中在 `pc/screen.py` 顶部的「可配置参数」区：

| 参数 | 默认值 | 含义 |
|------|--------|------|
| `PRICE_MIN` / `PRICE_MAX` | 3.0 / 30.0 | 股价区间（元） |
| `NMC_MIN` / `NMC_MAX` | 300000 / 2000000 | 流通市值区间（万元） |
| `SIGNAL_WINDOW` | 20 | 信号观察窗口（交易日） |
| `GOLDEN_CROSS_TARGET` | 2 | 窗口内需达到的金叉次数 |
| `MACD_FAST` / `MACD_SLOW` / `MACD_SIGNAL` | 6 / 13 / 5 | MACD 快线 / 慢线 / 信号线周期 |
| `K_DAYS` | 60 | 计算均线所需的最少 K 线根数 |
| `K_FETCH_DAYS` | 500 | 实际拉取的日 K 根数（支持历史回溯） |
| `SLEEP_SEC` | 0.3 | 每次 K 线请求的间隔（秒） |

---

## Android 版

**环境**：JDK 17、Android SDK（platform 34 + build-tools 34.0.0）、Gradle 8.7。

双击项目根目录的 **`build_android.bat`** 即可。脚本会自动探测本机 JDK / SDK / Gradle：

| 组件 | 探测顺序 |
|------|----------|
| JDK | `JAVA_HOME` → `%LOCALAPPDATA%\jdk-*` → `C:\Program Files\Java\jdk*` → `Eclipse Adoptium` → `D:\Java\jdk*` |
| SDK | `ANDROID_HOME` → `ANDROID_SDK_ROOT` → `%LOCALAPPDATA%\Android\Sdk` → `D:\Android\Sdk` |
| Gradle | `GRADLE_HOME` → `%USERPROFILE%\gradle-8.7` → `%USERPROFILE%\gradle-*` → `C:\Gradle\gradle-*` → `PATH` |

若三者都已设置环境变量则优先使用。找不到时脚本会明确报错并暂停（不会一闪而过）。

或用 Android Studio 直接打开 `android/` 目录构建。

> 仓库未包含 Gradle Wrapper，需自备 Gradle 8.7。构建脚本已配置阿里云 Maven 镜像，国内网络可直接拉依赖。

> **维护提示**：两个 `.bat` 均为 **ASCII 纯英文 + CRLF 换行**，请勿用 UTF-8 或 LF 保存。
> 含中文的 `.bat` 受代码页影响极易乱码，且纯 LF 换行会导致 CMD 解析错乱、双击闪退。

### 特点

- **后台运行**：筛选跑在前台 Service 里，点开始后可以退出应用，完成后通知栏提醒；
- **并发拉取**：4 线程并行请求，比 PC 版单线程快不少；
- **界面**：原生 WebView 加载本地 HTML，无第三方 UI 框架，APK 体积小；
- 结果以 JSON 存在应用私有目录，界面点击「刷新结果」即可查看。

筛选参数常量集中在 `android/app/src/main/java/com/example/stockscreen/StockScreen.kt` 顶部
（`WINDOW` / `TARGET` / `MACD_FAST` / `MACD_SLOW` / `MACD_SIGNAL`）。

---

## 数据接口

均来自新浪财经公开接口，无需鉴权。

| 用途 | 接口 |
|------|------|
| 股票列表 | `vip.stock.finance.sina.com.cn/.../Market_Center.getHQNodeData`（`node=hs_a`，分页 100 条） |
| 市场总数 | `vip.stock.finance.sina.com.cn/.../Market_Center.getHQNodeStockCount` |
| 日 K 线 | `quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData`（`scale=240` 日线，`datalen` 最大 500） |

列表接口需要带 `Referer: https://finance.sina.com.cn`；K 线接口只需 `User-Agent`。

---

## 已知限制

- **流通市值存在前视偏差**。列表接口返回的是**当前**流通市值，历史回溯时无法还原基准日的市值，
  因此用历史日期筛选出来的结果，市值条件实际按「今天的市值」判定。代码注释中已标为「当前值近似」。
- **涨停按收盘涨幅近似判定**。使用「收盘价较前一日涨幅 ≥ 9.8%」而非真实涨停价，未考虑新股上市、
  复牌等特殊涨跌幅情形。
- **MACD 的 EMA 以首根收盘价为种子**，而非标准的 SMA 初始化，与部分行情软件的数值会有微小差异。
- **MACD 使用 6 / 13 / 5 参数**（快线 EMA6、慢线 EMA13、信号线 DEA=EMA5），非默认的 12 / 26 / 9，
  信号会比标准参数更灵敏、金叉出现更早。
- 依赖第三方公开接口，接口变更或限流会导致失败；已内置 3 次重试与请求间隔。

---

## 免责声明

本项目仅用于技术学习与数据筛选实践。筛选结果不构成任何投资建议，
据此操作产生的盈亏由使用者自行承担。

## License

MIT
