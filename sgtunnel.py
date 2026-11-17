# -*- coding: utf-8 -*-
"""
爽哥面板隧道助手 V2.3
一键建立 SSH 隧道访问服务器管理面板

认证方式:
  1) 密钥认证: 使用 -i 私钥文件
  2) 密码认证: 通过 SSH_ASKPASS 辅助程序递交密码
     - 密码不出现在命令行参数中, 也不以明文落盘
     - 需要记住密码时, 用 Windows DPAPI 加密后以 Base64 密文存 config.json 的
       password_enc 字段(绑定当前 Windows 用户账户), 启动时解密回填密码框
     - 明文口令只在内存与 ssh 子进程环境变量中存在

界面: 右下角赞助支持卡片(纯展示), 二维码原图 assets/sponsor_qr.png,
      可选 Pillow 缩放(缺失时退回 tk 原生 PNG 解码), 打包经 spec 的 datas 内嵌。

用法: python sgtunnel.py
"""
import base64
import ctypes
import os
import json
import math
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import ttk

# ===== 版本 =====
APP_NAME = "爽哥面板隧道助手"
APP_VER = "2.3"

# ===== 路径与常量 =====
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)   # exe 同目录
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
REMOTE_HOST = "127.0.0.1"   # 隧道远端固定为本机回环

# 赞助卡片资源(V2.3): 二维码原图随包分发, 显示时长边缩到 SPONSOR_QR_MAX 以内
SPONSOR_QR_NAME = "sponsor_qr.png"
SPONSOR_QR_MAX = 125
SPONSOR_TEXT_WRAP = 168   # 文案换行宽度(卡片内容宽, 决定行数/卡片高度)
SPONSOR_TEXT = ("一元不嫌少，十元不嫌多。\n"
                "无论金额多少，都是对本软件项目的认可。\n"
                "赞助属于自愿行为，不购买任何功能、不享有售后服务，仅表达您的一份善意。")

# 按钮文案(V2.3): 带 emoji 的完整文案在这里集中声明。
# 首屏先用纯中文占位创建按钮, 窗口可见后再回填 emoji 完整文案——
# emoji 首次渲染要触发系统 emoji 字体回退初始化(本机实测 200~250 ms),
# 放在 __init__ 里会白白拖慢首屏, 放到 after 回调后窗口能早 200 ms 左右出现。
BTN_TEXTS = {
    "start": "🚀  启动隧道",
    "stop": "⏹  停止隧道",
    "open": "🌐  打开面板",
    "save": "💾  保存配置",
    "reload": "🔄  重新加载",
}

# 应用图标资源(V2.3): 主窗口图标双通道加载, exe 文件图标由 spec 的 icon= 负责
ICON_ICO_NAME = "app.ico"     # 多尺寸 ICO(16/24/32/48/64/128/256), 通道一首选
ICON_PNG_NAME = "app.png"     # 256px PNG, 通道二兜底

# ===== 体验与健壮性参数(V2.3 体验优化轮) =====
LOG_MAX_LINES = 2000        # 日志区最大保留行数: 超出丢最旧行, 防长时间运行内存膨胀
TICK_IDLE_MS = 5000         # 空闲(隧道未运行)时轮询间隔: 只做一次最轻量的状态判定
TICK_RUN_MS = 2000          # 运行中轮询间隔(端口探测在后台线程执行, 不阻塞界面)
DRAIN_START_MS = 100        # 消息泵初始间隔
DRAIN_FAST_MS = 30          # 有新消息后的下一次间隔(保持日志滚动顺滑)
DRAIN_IDLE_MS = 250         # 连续空闲时的间隔上限(自适应退避, 不再固定 100ms 空转)
DRAIN_BATCH = 200           # 单轮消息泵最多处理条数(ssh 刷屏时也不让界面卡顿)

# 状态四态(未运行 / 启动中 / 已运行 / 出错) + 已停止(idle 的变体), 文案与颜色统一收口;
# 具体样式表 STATE_STYLE 定义在配色常量之后(需引用 BTN_OK/BTN_STOP)
STATE_IDLE = "idle"
STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_ERROR = "error"
STATE_STOPPED = "stopped"


def asset_path(name):
    """资源路径三档兜底, 返回首个存在的绝对路径, 全不存在返回 None。

    ① sys._MEIPASS/assets/  (PyInstaller 单文件解包目录, 打包后首选)
    ② BASE_DIR/assets/      (exe 同级 / 源码根目录)
    ③ 源码文件所在目录/assets/ (源码态与 BASE_DIR 相同则不重复尝试)
    """
    cands = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cands.append(os.path.join(meipass, "assets", name))
    cands.append(os.path.join(BASE_DIR, "assets", name))
    src_dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.normcase(src_dir) != os.path.normcase(BASE_DIR):
        cands.append(os.path.join(src_dir, "assets", name))
    for path in cands:
        try:
            if os.path.isfile(path):
                return path
        except Exception:   # 非法路径字符等, 继续下一档
            continue
    return None


def atomic_write_text(path, text):
    """原子写文本文件: 同目录临时文件 → fsync → os.replace, 断电不会留下半截文件。

    阻塞点只有一次小文件写入, 失败时清理临时文件并把异常抛给调用方。
    """
    target = os.path.abspath(path)
    folder = os.path.dirname(target) or "."
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(target) + ".", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise


# 认证方式取值(写入 config.json 的 auth_method 字段, 非敏感)
AUTH_KEY = "key"
AUTH_PASSWORD = "password"

# 密码认证辅助脚本: ssh 会以 SSH_ASKPASS 指定的程序读取口令,
# 程序把密码写到 stdout, 密码来自环境变量 SG_TUNNEL_PW, 因此不出现在命令行参数中。
# 优先用 powershell 按 UTF-8 输出(可含 !"^&|<> 等特殊字符与中文); powershell 不可用时退回 cmd 延迟展开。
ASKPASS_SCRIPT = """@echo off
setlocal EnableDelayedExpansion
powershell -NoProfile -NonInteractive -Command "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; [Console]::Out.Write($env:SG_TUNNEL_PW)" 2>nul
if errorlevel 1 echo(!SG_TUNNEL_PW!
endlocal
"""

# ===== Windows DPAPI(数据保护 API)密码加密存储 =====
# 只用 ctypes 直接调用 crypt32/kernel32, 不引入 pywin32 / pycryptodome 等第三方依赖。
# DPAPI 密文与"当前 Windows 用户账户"绑定: 换用户、换机器、重装系统后无法解密(此时按失效处理)。
CRYPTPROTECT_UI_FORBIDDEN = 0x01   # 禁止弹出任何 UI(含密码提示框), 失败直接返回错误码
CRYPTPROTECT_LOCAL_MACHINE = 0x04  # 未使用: 不绑定机器级, 保持"仅当前用户可解密"的语义


class _DATA_BLOB(ctypes.Structure):
    """Windows DATA_BLOB 结构: 长度 + 数据指针"""
    _fields_ = [("cbData", ctypes.c_ulong),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


_DPAPI_HANDLES = []


def _dpapi_load():
    """惰性加载 crypt32/kernel32 并声明函数签名; 非 Windows 返回 None"""
    if _DPAPI_HANDLES:
        return _DPAPI_HANDLES
    if os.name != "nt":
        return None
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DATA_BLOB),      # pDataIn
            ctypes.c_wchar_p,                # szDataDescr(可空)
            ctypes.POINTER(_DATA_BLOB),      # pOptionalEntropy
            ctypes.c_void_p,                 # pvReserved
            ctypes.c_void_p,                 # pPromptStruct
            ctypes.c_ulong,                  # dwFlags
            ctypes.POINTER(_DATA_BLOB),      # pDataOut
        ]
        crypt32.CryptProtectData.restype = ctypes.c_int

        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DATA_BLOB),      # pDataIn
            ctypes.POINTER(ctypes.c_wchar_p),  # ppszDataDescr
            ctypes.POINTER(_DATA_BLOB),      # pOptionalEntropy
            ctypes.c_void_p,                 # pvReserved
            ctypes.c_void_p,                 # pPromptStruct
            ctypes.c_ulong,                  # dwFlags
            ctypes.POINTER(_DATA_BLOB),      # pDataOut
        ]
        crypt32.CryptUnprotectData.restype = ctypes.c_int

        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
    except Exception:
        return None
    _DPAPI_HANDLES.extend([crypt32, kernel32])
    return _DPAPI_HANDLES


def _blob_from(raw):
    """bytes -> (DATA_BLOB, 保持底层缓冲存活的对象)"""
    buf = ctypes.create_string_buffer(raw, len(raw))
    blob = _DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    return blob, buf


def dpapi_encrypt(plain):
    """明文口令 -> Base64(DPAPI 密文); 失败返回 None(调用方按"未保存密码"处理)"""
    if not plain:
        return None
    handles = _dpapi_load()
    if not handles:
        return None
    crypt32, kernel32 = handles
    raw = plain.encode("utf-8")
    blob_in, _keep = _blob_from(raw)
    blob_out = _DATA_BLOB(0, None)
    ok = crypt32.CryptProtectData(ctypes.byref(blob_in), None, None, None, None,
                                  CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        return None
    try:
        cipher = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        if blob_out.pbData:
            kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))
    return base64.b64encode(cipher).decode("ascii")


def dpapi_decrypt(token):
    """Base64(DPAPI 密文) -> 明文口令; 任何失败(Base64 坏、非本用户、数据损坏)返回 None"""
    token = str(token or "").strip()
    if not token:
        return None
    handles = _dpapi_load()
    if not handles:
        return None
    crypt32, kernel32 = handles
    try:
        cipher = base64.b64decode(token, validate=True)
    except Exception:
        return None
    if not cipher:
        return None
    blob_in, _keep = _blob_from(cipher)
    blob_out = _DATA_BLOB(0, None)
    ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None,
                                    CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        return None
    try:
        raw = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        if blob_out.pbData:
            kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))
    try:
        return raw.decode("utf-8")
    except Exception:
        return None

BG = "#1e1e2e"
FG = "#cdd6f4"
BTN = "#313244"
BTN_OK = "#a6e3a1"
BTN_STOP = "#f38ba8"
BTN_HL = "#89b4fa"
TXT = "#cdd6f4"
CARD = "#313244"             # 卡片底(比主背景浅一档, 与按钮同色系)
TEXT_SECONDARY = "#a6adc8"   # 次要文字(与日志区正文同色)
BTN_DIRTY = "#f9e2af"        # 有未保存改动时『保存配置』按钮的底色(提醒态, 不改尺寸)

# 四态样式表: 文案 + 颜色一一绑定, 状态文字与状态色永远同源, 不会再出现"文字已变、颜色没变"
STATE_STYLE = {
    STATE_IDLE:     ("● 隧道未运行", "#a6adc8"),        # 灰: 未运行
    STATE_STARTING: ("● 隧道启动中...", "#f9e2af"),     # 黄: 启动中
    STATE_RUNNING:  ("● 隧道已连接 ✅", BTN_OK),        # 绿: 已运行
    STATE_ERROR:    ("● 隧道出错, 详见日志", BTN_STOP),  # 红: 出错
    STATE_STOPPED:  ("● 隧道已停止", "#a6adc8"),        # 灰: 用户主动停止
}


class TunnelApp:
    WIN_W = 640              # 窗口宽(布局实测需求 615px)
    WIN_H = 820              # 窗口高(布局实测需求 813px)

    def __init__(self, root):
        self._t0 = time.perf_counter()   # 首屏耗时统计起点
        self.root = root
        self.proc = None
        self.running = False
        self.panels = []
        self.panel_names = []
        self._last_panel = None
        self._askpass_dir = None
        self._askpass_path = None
        # 后台线程 -> 主线程的消息队列(后台线程绝不直接操作 Tk 控件)
        self._ui_q = queue.Queue()

        # ===== 体验优化状态(V2.3) =====
        self._busy = False            # 启动/停止处理中: 按钮立即置灰, 防重复点击
        self._state = STATE_IDLE      # 四态状态机: idle / starting / running / error(+stopped)
        self._dirty = False           # 界面是否有未保存的编辑内容
        self._suppress_dirty = False  # 程序自身回填界面时为 True, 不计入脏标记
        self._log_follow = True       # 日志跟随最新一行(用户上滚查看历史时暂停跟随)
        self._probe_busy = False      # 端口探测是否在跑(防线程堆积)
        self._drain_id = None         # 消息泵 after id(便于自适应重排)
        self._drain_interval = DRAIN_START_MS
        self._tick_id = None          # 定时器 after id(便于自适应重排)

        root.title(f"{APP_NAME} v{APP_VER}")
        # 2026-11-06 按要求去掉主窗口标题栏左上角图标:
        #   ① 自定义盾牌图标已在 _deferred_init 步骤表里停用(见该处注释);
        #   ② 再由 _clear_titlebar_icon 给窗口塞全透明 HICON 顶掉大小图标 ——
        #      只发 WM_SETICON(lParam=0) 会回落到 Tk 内建默认图标(羽毛)或空白黑块。
        #   打包版 exe 内嵌 app.ico, Tk 映射完成后还会再设一次, 故方法内部自带
        #   WM_GETICON 校验 + 400ms 重试(最多 8 次); 这里先按 60/300ms 起两次头。
        # 失败不影响启动, 仅日志可查; 需要恢复图标时删掉下面两行调度并取消步骤表注释即可。
        root.after(60, self._clear_titlebar_icon)
        root.after(300, self._clear_titlebar_icon)
        # 相对屏幕居中: 先算屏幕尺寸再定位, 避免窗口总是开在左上角
        root.geometry(self._center_geometry(self.WIN_W, self.WIN_H))
        root.minsize(self.WIN_W, self.WIN_H)   # 显式下限: 赞助卡片加入后布局需求 813px, 锁死尺寸防裁剪
        root.configure(bg=BG)
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        # ===== 标题 =====
        # 标题 emoji 同在首屏后回填(见 _restore_deferred_texts), 首屏先上纯文字版
        self.title_label = tk.Label(root, text=f"{APP_NAME} v{APP_VER}",
                                    font=("Microsoft YaHei UI", 18, "bold"), bg=BG, fg=BTN_HL)
        self.title_label.pack(pady=(12, 2))
        tk.Label(root, text="SSH 隧道访问服务器管理面板", font=("Microsoft YaHei UI", 10),
                 bg=BG, fg=FG).pack()

        # ===== 配置区 =====
        cfg = tk.LabelFrame(root, text=" 隧道配置 ", font=("Microsoft YaHei UI", 10, "bold"),
                            bg=BG, fg=BTN_HL, bd=1, relief="solid")
        cfg.pack(fill="x", padx=20, pady=(14, 6))

        # 界面值完全由 config.json 驱动, 初始留空
        self.var_ip = tk.StringVar()
        self.var_port = tk.StringVar()
        self.var_user = tk.StringVar()
        self.var_key = tk.StringVar()
        self.var_pwd = tk.StringVar()
        self.var_auth = tk.StringVar(value=AUTH_KEY)
        self.var_lport = tk.StringVar()
        self.var_rport = tk.StringVar()

        # 未保存修改追踪: 用户手工编辑任一输入项即置脏, 程序回填时由 _suppress_dirty 屏蔽
        for _var in (self.var_ip, self.var_port, self.var_user, self.var_key,
                     self.var_pwd, self.var_lport, self.var_rport):
            _var.trace_add("write", self._on_var_edited)

        def row(label, var, show=None, disabled=False):
            f = tk.Frame(cfg, bg=BG)
            f.pack(fill="x", padx=10, pady=3)
            tk.Label(f, text=label, width=10, anchor="w", font=("Microsoft YaHei UI", 10),
                     bg=BG, fg=FG).pack(side="left")
            e = tk.Entry(f, textvariable=var, font=("Consolas", 10), bg=BTN, fg=TXT,
                         insertbackground=FG, relief="flat", show=show)
            e.pack(side="left", fill="x", expand=True)
            if disabled:
                e.config(state="disabled")
            return e

        row("服务器 IP", self.var_ip)
        row("SSH 端口", self.var_port)
        row("用户名", self.var_user)

        # 认证方式: 密钥认证 / 密码认证 二选一
        fa = tk.Frame(cfg, bg=BG)
        fa.pack(fill="x", padx=10, pady=3)
        tk.Label(fa, text="认证方式", width=10, anchor="w", font=("Microsoft YaHei UI", 10),
                 bg=BG, fg=FG).pack(side="left")
        for text, val in (("密钥认证", AUTH_KEY), ("密码认证", AUTH_PASSWORD)):
            tk.Radiobutton(fa, text=text, variable=self.var_auth, value=val,
                           command=self._on_auth_change, font=("Microsoft YaHei UI", 10),
                           bg=BG, fg=FG, selectcolor=BTN, activebackground=BG,
                           activeforeground=BTN_HL, bd=0, highlightthickness=0,
                           ).pack(side="left", padx=(0, 14))

        self.ent_key = row("密钥文件", self.var_key)
        self.ent_pwd = row("密码", self.var_pwd, show="*", disabled=True)

        # 面板选择: 多面板切换, 本地/远程端口随选择自动填充
        f = tk.Frame(cfg, bg=BG)
        f.pack(fill="x", padx=10, pady=3)
        tk.Label(f, text="面板选择", width=10, anchor="w", font=("Microsoft YaHei UI", 10),
                 bg=BG, fg=FG).pack(side="left")
        self.var_panel = tk.StringVar()
        self.cmb_panel = ttk.Combobox(f, textvariable=self.var_panel, state="readonly",
                                      font=("Microsoft YaHei UI", 10))
        self.cmb_panel.pack(side="left", fill="x", expand=True)
        self.cmb_panel.bind("<<ComboboxSelected>>", self._on_panel_change)

        row("本地端口", self.var_lport)
        row("远程端口", self.var_rport)

        # ===== 状态区 =====
        self.status = tk.Label(root, text="● 隧道未运行", font=("Microsoft YaHei UI", 12, "bold"),
                               bg=BG, fg="#f9e2af")
        self.status.pack(pady=(8, 2))

        # ===== 按钮区(大按钮, 方便脚操作) + 赞助卡片(右侧, 纯展示) =====
        mid = tk.Frame(root, bg=BG)
        mid.pack(fill="x", padx=20, pady=(6, 2))

        btns = tk.Frame(mid, bg=BG)
        btns.pack(side="left")

        self.btn_start = tk.Button(btns, text="启动隧道", command=self.start_tunnel,
                                   font=("Microsoft YaHei UI", 16, "bold"), bg=BTN_OK, fg="#11111b",
                                   width=12, relief="flat", activebackground="#94e2d5")
        self.btn_start.grid(row=0, column=0, padx=8, pady=6)

        self.btn_stop = tk.Button(btns, text="停止隧道", command=self.stop_tunnel,
                                  font=("Microsoft YaHei UI", 16, "bold"), bg=BTN_STOP, fg="#11111b",
                                  width=12, relief="flat", state="disabled", activebackground="#fab387")
        self.btn_stop.grid(row=0, column=1, padx=8, pady=6)

        self.btn_open = tk.Button(btns, text="打开面板", command=self.open_panel,
                                  font=("Microsoft YaHei UI", 16, "bold"), bg=BTN_HL, fg="#11111b",
                                  width=12, relief="flat", state="disabled", activebackground="#cba6f7")
        self.btn_open.grid(row=1, column=0, columnspan=2, pady=6)

        self.btn_save = tk.Button(btns, text="保存配置", command=self.save_config,
                                  font=("Microsoft YaHei UI", 14, "bold"), bg="#cba6f7", fg="#11111b",
                                  width=12, relief="flat", activebackground="#f5e0dc")
        self.btn_save.grid(row=2, column=0, padx=8, pady=6)

        self.btn_reload = tk.Button(btns, text="重新加载", command=self.load_config,
                                    font=("Microsoft YaHei UI", 14, "bold"), bg="#94e2d5", fg="#11111b",
                                    width=12, relief="flat", activebackground="#f9e2af")
        self.btn_reload.grid(row=2, column=1, padx=8, pady=6)

        # ===== 日志区 =====
        # 常态 disabled, 但仍可选文本; 右键菜单 + Ctrl+C/Ctrl+A 均可复制(见 _build_log_menu)
        self.log_box = tk.Text(root, height=5, font=("Consolas", 9), bg="#11111b", fg="#a6adc8",
                               state="disabled", relief="flat", padx=6, pady=6, wrap="none")
        self.log_box.pack(fill="both", expand=True, padx=20, pady=(4, 10))
        self._build_log_menu()

        # ===== 赞助支持卡片(纯展示: 不绑定命令, 不参与按钮启用/禁用) =====
        # 必须放在日志区之后: 图片加载失败的告警日志要写进 log_box
        self._build_sponsor_card(mid)

        # ===== 首屏提速(V2.3) =====
        # 图标加载、二维码解码(PIL 导入)、config.json 读取 + DPAPI 解密都是几十~几百毫秒级
        # 阻塞项, 一律推迟到 after 回调执行, 让窗口先可见可交互。
        self._set_state(STATE_IDLE)
        self._refresh_buttons()
        # 先把窗口真正映射到屏幕(只处理布局/绘制的 idle 事件, 不跑定时器回调),
        # 再推迟图标/二维码/配置等耗时项: 用户看到窗口的时间由首个 after 回调决定,
        # 不先刷一次屏的话, emoji 回填等定时任务会抢在窗口上屏前跑, 首屏反而更慢。
        try:
            self.root.update_idletasks()
        except Exception:
            pass
        self.root.after(1, self._deferred_init)

    # ---------- 首屏后的初始化 ----------
    def _center_geometry(self, w, h):
        """按屏幕尺寸算居中坐标, 返回 Tk geometry 字符串 'WxH+X+Y'。"""
        try:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
        except Exception:
            return f"{w}x{h}"
        return f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 2)}"

    def _deferred_init(self):
        """首屏渲染完成后再做的初始化。

        逐项独立 try: 任何一项失败只写日志, 不阻断后续步骤, 也不影响消息泵启动。
        顺序有意为之——图标/二维码先上屏提升观感, 配置读取放在其后。
        """
        build_ms = (time.perf_counter() - self._t0) * 1000
        # ① 先把按钮 emoji 文案补齐(界面完整度优先), 再依次做图标/二维码/配置。
        try:
            self._restore_deferred_texts()
        except Exception as e:
            self.log(f"按钮文案回填失败(已跳过, 不影响使用): {e}")
        self.root.update_idletasks()
        texts_ms = (time.perf_counter() - self._t0) * 1000
        steps = (
            # V2.3 修订(2026-11-06): 按要求移除主窗口标题栏左上角图标, 窗口改用系统默认图标。
            # 方法 _apply_window_icon 及其 png/ico 双通道保留未删, 需要恢复时把下面这行取消注释即可。
            # ("窗口图标", self._apply_window_icon),
            ("赞助二维码", self._mount_sponsor_qr),
            ("配置加载", self.load_config),
        )
        for name, step in steps:
            try:
                step()
            except Exception as e:
                self.log(f"{name}初始化失败(已跳过, 不影响使用): {e}")
        try:
            self._on_auth_change()
        except Exception as e:
            self.log(f"认证方式联动初始化失败: {e}")
        self._scroll_log_to_end()
        # ===== 后台消息泵 + 定时检查 =====
        self._drain_id = self.root.after(self._drain_interval, self._drain_ui)
        self._tick()
        total_ms = (time.perf_counter() - self._t0) * 1000
        self.log(f"界面就绪: 首屏构建 {build_ms:.0f} ms, 按钮文案 {texts_ms - build_ms:.0f} ms, "
                 f"含延迟初始化共 {total_ms:.0f} ms")

    def _restore_deferred_texts(self):
        """首屏后回填带 emoji 的文案(标题 + 五个按钮)。

        emoji 首渲染触发系统字体回退初始化, 本机实测标题第一个 emoji 就要 200+ ms;
        放在 __init__ 里等于把首屏拖慢同一量级, 放到 after 回调后窗口先出来再补文案。
        标题字号/按钮 width(12 字符) 均固定, 文案变化不引起布局抖动。
        """
        # 标题 emoji 已按用户要求去除(2026-11-06): 该 emoji 走字体回退单色渲染,
        # 在深色底上显示为一枚蓝色小图形, 与标题纯文字风格不一致。
        # 如需恢复, 把下面的文案改回 f"🛰️  {APP_NAME} v{APP_VER}" 即可。
        try:
            self.title_label.config(text=f"{APP_NAME} v{APP_VER}")
        except Exception:
            pass
        pairs = (
            ("btn_start", BTN_TEXTS["start"]),
            ("btn_stop", BTN_TEXTS["stop"]),
            ("btn_open", BTN_TEXTS["open"]),
            ("btn_save", BTN_TEXTS["save"]),
            ("btn_reload", BTN_TEXTS["reload"]),
        )
        for attr, text in pairs:
            btn = getattr(self, attr, None)
            if btn is None:
                continue
            try:
                btn.config(text=text)
            except Exception:
                pass

    # ---------- 赞助支持卡片(V2.3) ----------
    def _build_sponsor_card(self, parent):
        """赞助卡片: 浅色卡片底 + 标题 + 三段文案 + 收款二维码。

        纯展示控件: 不绑定任何命令、不参与运行状态矩阵、不受 _busy/按钮启停影响。
        图片缺失、损坏或两条加载通道均失败时只写一条日志并保留纯文字卡片,
        不弹窗、不抛异常、卡片不塌陷。
        """
        card = tk.Frame(parent, bg=CARD, padx=8, pady=7)
        card.pack(side="right", anchor="n")
        self.sponsor_card = card

        tk.Label(card, text="赞助支持", font=("Microsoft YaHei UI", 9, "bold"),
                 bg=CARD, fg=BTN_HL).pack(anchor="center")
        tk.Label(card, text=SPONSOR_TEXT, font=("Microsoft YaHei UI", 8),
                 bg=CARD, fg=TEXT_SECONDARY, justify="center", anchor="center",
                 wraplength=SPONSOR_TEXT_WRAP).pack(anchor="center", pady=(3, 0))

        # PhotoImage 必须挂到实例属性上, 否则被 GC 回收后图片变空白
        self._sponsor_qr_photo = None
        # 图片解码(PIL 导入 + 缩放)耗时, 推迟到 _deferred_init 里执行:
        # 先让纯文字卡片上屏, 首屏不再为一张 250px 图等待。

    def _mount_sponsor_qr(self):
        """把二维码挂到已建好的卡片上(首屏后执行), 任何失败只写一条日志。"""
        card = getattr(self, "sponsor_card", None)
        if card is None:
            return
        try:
            self._load_sponsor_qr(card)
        except Exception as e:   # 最后一道防线: 卡片异常不得影响主界面构建
            self._sponsor_warn(f"赞助二维码加载失败, 已跳过显示: {e}")

    def _sponsor_warn(self, msg):
        """卡片专用日志: log_box 尚未创建等极端情形都只丢弃, 不抛异常。"""
        try:
            if getattr(self, "log_box", None) is not None:
                self.log(msg)
        except Exception:
            pass

    def _load_sponsor_qr(self, parent):
        """解析资源路径并加载二维码, 全部失败时只记一条日志。"""
        path = asset_path(SPONSOR_QR_NAME)
        if not path:
            self._sponsor_warn(f"赞助二维码图片不存在, 已跳过显示(卡片仅保留文字): assets{os.sep}{SPONSOR_QR_NAME}")
            return
        photo = self._qr_via_pil(path)
        if photo is None:
            photo = self._qr_via_tk(path)
        if photo is None:
            self._sponsor_warn(f"赞助二维码加载失败(两条通道均不可用), 已跳过显示: {path}")
            return
        self._sponsor_qr_photo = photo
        self.sponsor_qr_label = tk.Label(parent, image=photo, bg=CARD,
                                         bd=0, highlightthickness=0)
        self.sponsor_qr_label.pack(anchor="center", pady=(5, 0))

    @staticmethod
    def _qr_via_pil(path):
        """通道一(可选增强): Pillow 打开 + LANCZOS 等比缩放, 最长边 <= SPONSOR_QR_MAX。

        Pillow 不是必需依赖: 导入失败/解码失败/上屏异常一律返回 None, 由调用方降级。
        """
        try:
            from PIL import Image, ImageTk
        except Exception:
            return None
        try:
            img = Image.open(path)
            img.load()
            img = img.convert("RGBA")   # 统一带透明通道, 与卡片底色融合
            img.thumbnail((SPONSOR_QR_MAX, SPONSOR_QR_MAX), Image.LANCZOS)
            return ImageTk.PhotoImage(img)
        except Exception:
            return None

    @staticmethod
    def _qr_via_tk(path):
        """通道二(零依赖兜底): tk.PhotoImage 原生 PNG 解码 + subsample 整数降采样。"""
        try:
            photo = tk.PhotoImage(file=path)
            longest = max(photo.width(), photo.height())
            if longest > SPONSOR_QR_MAX:
                factor = int(math.ceil(longest / float(SPONSOR_QR_MAX)))
                photo = photo.subsample(max(1, factor), max(1, factor))
            return photo
        except Exception:
            return None

    # ---------- 主窗口图标(V2.3) ----------
    def _make_blank_hicon(self, size):
        """造一个全透明 HICON(32bpp 全 0 彩色位图 + 全 1 AND 掩码), 用于顶掉标题栏图标。

        注意: 只 WM_SETICON(lParam=0) 是没用的——窗口图标清空后 Windows 会回落到
        TkTopLevel 窗口类自带图标(羽毛), 所以必须塞一个"看得见但全透明"的图标句柄。
        掩码行必须按 2 字节对齐, 否则掩码无效, 标题栏会画出黑块。
        """
        import ctypes
        from ctypes import wintypes

        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32

        class ICONINFO(ctypes.Structure):
            _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                        ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                        ("hbmColor", wintypes.HBITMAP)]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD),
                        ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG),
                        ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                        ("bmiColors", wintypes.DWORD * 3)]

        hdc = user32.GetDC(None)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = size
        bmi.bmiHeader.biHeight = -size      # 负值 = 自上而下, 免翻转
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        bits = ctypes.c_void_p()
        hbm_color = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0,
                                           ctypes.byref(bits), None, 0)
        ctypes.memset(bits, 0, size * size * 4)     # 像素全 0 => alpha=0 全透明
        stride = ((size + 15) // 16) * 2
        mask_buf = (ctypes.c_ubyte * (stride * size))()
        for i in range(stride * size):
            mask_buf[i] = 0xFF                      # 掩码全 1 => 不绘制任何像素
        hbm_mask = gdi32.CreateBitmap(size, size, 1, 1, mask_buf)
        user32.ReleaseDC(None, hdc)

        info = ICONINFO()
        info.fIcon = 1
        info.hbmColor = hbm_color
        info.hbmMask = hbm_mask
        hicon = user32.CreateIconIndirect(ctypes.byref(info))
        return hicon, hbm_color, hbm_mask

    def _ensure_blank_icons(self):
        """懒造并缓存 16/32 两个全透明 HICON(只造一次, 反复发消息时复用同一批句柄)。"""
        icons = getattr(self, "_blank_icons", None)
        if icons:
            return icons
        icons = []
        for size in (16, 32):
            icons.append((size,) + self._make_blank_hicon(size))
        self._blank_icons = icons
        return icons

    def _clear_titlebar_icon(self, attempt=1):
        """移除主窗口标题栏左上角图标(2026-11-06 按要求)。

        做法: 造全透明 HICON 后对 ICON_SMALL(0)/ICON_BIG(1) 各发一次 WM_SETICON,
        标题栏左侧即不显示任何图标, 也不留黑块占位。
        句柄挂到实例属性上保活(GC 回收后标题栏会重新长出默认图标)。

        实测(2026-11-06 打包版): exe 内嵌了 app.ico, Tk 在窗口映射完成后会用自己的
        时机再设一次窗口图标, 只清一次(60/300ms)会被覆盖掉 —— 所以这里用 WM_GETICON
        回读校验, 没顶掉就在 400ms 后重试, 最多 8 次(约 3s), 幂等且失败只写日志。
        仅 Windows 生效; 非 Windows 或失败不影响主流程。
        """
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            WM_SETICON, WM_GETICON = 0x0080, 0x007F
            hwnd = self.root.winfo_id()
            parent = user32.GetParent(wintypes.HWND(hwnd))  # 真 HWND 是 TkChild 的父窗口
            if parent:
                hwnd = parent
            pending = False
            # 顺序固定为 16px / 32px, 恰好对应 ICON_SMALL(0) / ICON_BIG(1)
            for idx, (size, hicon, _hc, _hm) in enumerate(self._ensure_blank_icons()):
                user32.SendMessageW(wintypes.HWND(hwnd), WM_SETICON,
                                    wintypes.WPARAM(idx), wintypes.LPARAM(hicon))
                if user32.SendMessageW(wintypes.HWND(hwnd), WM_GETICON,
                                       wintypes.WPARAM(idx), 0) != hicon:
                    pending = True            # 被系统/Tk 覆盖回去了, 稍后重试
            if pending and attempt < 8:
                self.root.after(400, lambda: self._clear_titlebar_icon(attempt + 1))
            elif pending:
                self.log("[警告] 标题栏图标多次清除未生效, 已放弃重试")
        except Exception as exc:
            self.log(f"[警告] 移除标题栏图标失败: {exc}")

    def _apply_window_icon(self):
        """主窗口图标双通道加载: ① app.ico(iconbitmap, default= 同步 Toplevel/弹窗)
        ② app.png(iconphoto, 零依赖 tk 原生解码, PIL 可用时先做 LANCZOS 缩放)。

        任一通道失败都只写一条日志, 不弹窗、不抛异常、不影响主流程;
        PhotoImage 必须挂到实例属性上, 否则被 GC 回收后图标变空白。
        """
        try:
            ico = asset_path(ICON_ICO_NAME)
            if ico:
                try:
                    # default= 让后续所有 Toplevel/弹窗继承同一图标
                    self.root.iconbitmap(default=ico)
                    self.root.iconbitmap(ico)
                    return
                except Exception as e:
                    self._icon_warn(f"窗口图标 ico 通道加载失败, 改用 png 兜底: {e}")
            else:
                self._icon_warn(f"图标文件不存在: assets{os.sep}{ICON_ICO_NAME}")

            png = asset_path(ICON_PNG_NAME)
            if not png:
                self._icon_warn(f"图标文件不存在: assets{os.sep}{ICON_PNG_NAME}, 窗口沿用默认图标")
                return
            photo = self._icon_via_pil(png) or self._icon_via_tk(png)
            if photo is None:
                self._icon_warn(f"窗口图标 png 通道加载失败(两条加载均不可用), 窗口沿用默认图标: {png}")
                return
            self._icon_photo = photo
            self.root.iconphoto(True, photo)   # True => 作为所有 Toplevel 的默认图标
        except Exception as e:   # 最后一道防线: 图标异常不得影响主界面
            self._icon_warn(f"窗口图标加载异常, 已忽略: {e}")

    def _icon_warn(self, msg):
        """图标专用日志: log_box 尚未创建等极端情形只丢弃, 不抛异常。"""
        try:
            if getattr(self, "log_box", None) is not None:
                self.log(msg)
        except Exception:
            pass

    @staticmethod
    def _icon_via_pil(path):
        """png 通道增强: Pillow 解码后转 ImageTk(可选依赖, 缺失返回 None)。"""
        try:
            from PIL import Image, ImageTk
        except Exception:
            return None
        try:
            img = Image.open(path)
            img.load()
            img = img.convert("RGBA")
            return ImageTk.PhotoImage(img)
        except Exception:
            return None

    @staticmethod
    def _icon_via_tk(path):
        """png 通道零依赖兜底: tk.PhotoImage 原生 PNG 解码。"""
        try:
            return tk.PhotoImage(file=path)
        except Exception:
            return None

    # ---------- 后台消息队列 ----------
    def _post(self, kind, *args):
        """后台线程向主线程投递消息(唯一允许的跨线程通道)"""
        self._ui_q.put((kind, args))

    def _drain_ui(self):
        """主线程消费后台消息: 所有 Tk 控件更新都发生在这里。

        间隔自适应: 有消息时 DRAIN_FAST_MS 保持流畅, 连续空闲则翻倍退避到
        DRAIN_IDLE_MS 上限, 避免固定 100 ms 空转白烧 CPU。
        单轮最多处理 DRAIN_BATCH 条, 防止 ssh 刷屏时一次吞太多把界面卡住。
        """
        self._drain_id = None
        handled = 0
        kind = "?"
        try:
            while handled < DRAIN_BATCH:
                kind, args = self._ui_q.get_nowait()
                handled += 1
                self._dispatch(kind, args)
        except queue.Empty:
            pass
        except Exception as e:  # 防御: 单条消息异常不应终止消息泵
            self._log_now(f"界面更新异常[{kind}]: {e}")
        if handled:
            self._drain_interval = DRAIN_FAST_MS
        else:
            self._drain_interval = min(DRAIN_IDLE_MS, self._drain_interval * 2)
        self._drain_id = self.root.after(self._drain_interval, self._drain_ui)

    def _dispatch(self, kind, args):
        """按消息类型分发(全部在主线程执行)。"""
        if kind == "log":
            self._log_now(args[0])
        elif kind == "state":
            self._set_state_now(args[0])
        elif kind == "exited":
            self._proc_exit()
        elif kind == "started":
            self._on_started()
        elif kind == "start_error":
            self._on_start_failed(args[0], args[1] if len(args) > 1 else None)
        elif kind == "probe":
            self._on_probe(args[0] if args else None)
        elif kind == "stopped":
            self._on_stopped()

    # ---------- 配置读写 ----------
    def load_config(self):
        """从 config.json 读取设置填充界面。
        字段为空(空字符串 / null / 缺失)时对应输入框保持留空, 不回填任何默认值。
        密码: 只接受 password_enc(DPAPI 密文)字段, 解密成功后回填密码框;
        解密失败(换机/换用户/密文损坏)则密码框留空、提示用户并删除失效密文。

        整段用 _suppress_dirty 包裹: 程序自身回填界面不算"用户未保存的修改"。
        """
        prev = self._suppress_dirty
        self._suppress_dirty = True
        try:
            self._load_config_impl()
        finally:
            self._suppress_dirty = prev
            self._dirty = False
            self._refresh_buttons()

    def _load_config_impl(self):
        # 先把界面值清空, 保证"文件为空则界面为空"
        for var in (self.var_ip, self.var_port, self.var_user, self.var_key,
                    self.var_lport, self.var_rport):
            var.set("")
        self.var_pwd.set("")
        self.var_auth.set(AUTH_KEY)

        if not os.path.exists(CONFIG_PATH):
            self.panels = [{"name": "面板1", "local_port": "", "remote_port": ""}]
            self._refresh_panel_values("面板1")
            self._last_panel = "面板1"
            self.log(f"未找到 config.json({CONFIG_PATH}), 界面保持空白; "
                     f"按提示填写后点『保存配置』会自动生成该文件")
            return
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            self.log(f"读取配置失败: {e}")
            self._notify("配置读取失败", f"{CONFIG_PATH} 解析失败({e})",
                         hints=["用记事本打开该文件, 确认是合法 JSON(引号/逗号/括号成对)",
                                "或删掉该文件后按界面填写内容点『保存配置』重新生成"])
            return
        if not isinstance(data, dict):
            self.log("读取配置失败: config.json 顶层不是对象")
            self._notify("配置读取失败", "config.json 顶层必须是 JSON 对象(以 { 开头, } 结尾)",
                         hints=["删掉该文件后按界面填写内容点『保存配置』重新生成"])
            return
        # 安全提示: 历史配置里若残留明文密码, 一律忽略且不加载到界面
        for bad in ("password", "passwd", "pwd"):
            if bad in data:
                self.log(f"注意: config.json 中的 {bad} 字段已被忽略(明文口令不落盘, "
                         f"请改用界面输入, 保存时会写成加密字段 password_enc)")
                break

        # 兼容旧格式: 无 panels 时视为单个面板(端口不填默认值)
        panels = data.get("panels")
        if not isinstance(panels, list) or len(panels) == 0:
            panels = [{
                "name": "面板1",
                "local_port": str(data.get("local_port") or "").strip(),
                "remote_port": str(data.get("remote_port") or "").strip(),
            }]
        # 规范化: name 唯一化 + 端口统一转字符串(空值保持空)
        norm, seen = [], {}
        for i, p in enumerate(panels):
            if not isinstance(p, dict):
                continue
            name = str(p.get("name") or "").strip() or f"面板{i+1}"
            if name in seen:
                seen[name] += 1
                name = f"{name}-{seen[name]}"
            else:
                seen[name] = 1
            norm.append({
                "name": name,
                "local_port": str(p.get("local_port") or "").strip(),
                "remote_port": str(p.get("remote_port") or "").strip(),
            })
        if not norm:
            norm = [{"name": "面板1", "local_port": "", "remote_port": ""}]
        self.panels = norm
        self.panel_names = [p["name"] for p in norm]

        # 选中面板
        cur = str(data.get("current") or "").strip()
        if cur not in self.panel_names:
            cur = self.panel_names[0]
        self._last_panel = cur
        self._refresh_panel_values(cur)
        for p in self.panels:
            if p["name"] == cur:
                self.var_lport.set(p["local_port"])
                self.var_rport.set(p["remote_port"])
                break

        # 顶层 SSH 公共参数: 空值/缺失一律留空
        for k, var in (("server", self.var_ip), ("ssh_port", self.var_port),
                       ("user", self.var_user)):
            val = str(data.get(k) or "").strip()
            if val:
                var.set(val)
        key_val = str(data.get("key_path") or "").strip()
        if key_val:
            # key_path 若是纯文件名, 自动定位到程序同目录, 便于整体搬动
            if not os.path.isabs(key_val) and os.sep not in key_val and "/" not in key_val:
                key_val = os.path.join(BASE_DIR, key_val)
            self.var_key.set(key_val)

        # 认证方式: 缺省按密钥认证(模式选择, 非输入值)
        am = str(data.get("auth_method") or "").strip().lower()
        self.var_auth.set(AUTH_PASSWORD if am == AUTH_PASSWORD else AUTH_KEY)

        # 密码: 只从 password_enc 解密回填, 明文永不落盘
        enc = str(data.get("password_enc") or "").strip()
        if "password_enc" in data and not enc:
            # 空串/空白等同"没有密码": 顺手清掉该字段, 保持配置文件整洁
            self.var_pwd.set("")
            self._drop_password_enc()
        elif enc:
            plain = dpapi_decrypt(enc)
            if plain:
                self.var_pwd.set(plain)
                self.log("已解密并回填密码(DPAPI 密文, 仅当前 Windows 用户可解)")
            else:
                # 换机器/换用户/密文损坏: 留空 + 可见提示 + 删除失效密文, 不阻断启动
                self.var_pwd.set("")
                dropped = self._drop_password_enc()
                self.log("密码解密失败(可能换了电脑/用户或密文已损坏), 密码框已留空"
                         + (", 失效的 password_enc 已从 config.json 删除"
                            if dropped else ", 但删除失效密文失败"))
                self.log("请重新输入密码并点击\"保存配置\"")
                self._warn_decrypt_failed(dropped)

        self.log(f"已从 config.json 加载配置, 当前面板: {cur}")

    def _warn_decrypt_failed(self, dropped):
        """解密失败的可见提示: 只写日志(不弹阻塞式 messagebox, 不打断主循环)。

        v2.3 体验优化: 原先用 after + messagebox 弹窗, 会打断用户操作且需手动关闭,
        改为日志两行 + 引导动作, 与其它错误提示保持同一套反馈口径。
        """
        try:
            self.log("密码密文无法解密(常见原因: 配置文件拷到了其它电脑 / 换了 Windows 登录用户 / 密文被改动)")
            self.log("已解决方式: 在『密码』框重新输入密码, 再点『保存配置』"
                     + ("(失效的 password_enc 已自动从 config.json 删除)"
                        if dropped else "(失效密文删除失败, 可手工删除 password_enc 字段)"))
        except Exception:
            pass

    def _drop_password_enc(self):
        """删除 config.json 中失效的 password_enc(保留其它字段原样); 成功返回 True"""
        if not os.path.exists(CONFIG_PATH):
            return False
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            self.log(f"清除失效密码密文失败: {e}")
            return False
        if not isinstance(data, dict) or "password_enc" not in data:
            return True
        data.pop("password_enc", None)
        try:
            # 原子写: 避免清理到一半断电把 config.json 写坏
            atomic_write_text(CONFIG_PATH,
                              json.dumps(data, ensure_ascii=False, indent=4))
        except Exception as e:
            self.log(f"清除失效密码密文失败: {e}")
            return False
        return True

    def _collect_config(self):
        """收集界面值: SSH 公共参数 + 面板列表(当前面板端口写回)。
        密码不落明文: 有值则 DPAPI 加密后写 password_enc; 密码框为空则不写该字段(等于删除)。
        """
        cur = self._resolve_panel_name(self.var_panel.get())
        if not cur and self.panel_names:
            cur = self.panel_names[0]
        if not cur:
            cur = "面板1"
        if not self.panels:
            self.panels = [{"name": cur, "local_port": "", "remote_port": ""}]
        found = False
        for p in self.panels:
            if str(p.get("name", "")).strip() == cur:
                p["local_port"] = self.var_lport.get().strip()
                p["remote_port"] = self.var_rport.get().strip()
                found = True
                break
        if not found:
            self.panels.append({
                "name": cur,
                "local_port": self.var_lport.get().strip(),
                "remote_port": self.var_rport.get().strip(),
            })
            self.panel_names = [str(x.get("name", "")).strip() for x in self.panels]
        auth = self.var_auth.get()
        # 密钥文件位于程序同目录时, 落盘只写文件名, 保持配置随目录整体搬动可用
        key_out = self.var_key.get().strip()
        if key_out:
            try:
                if os.path.normcase(os.path.dirname(os.path.abspath(key_out))) == \
                        os.path.normcase(os.path.abspath(BASE_DIR)):
                    key_out = os.path.basename(key_out)
            except Exception:
                pass
        return {
            "server": self.var_ip.get().strip(),
            "ssh_port": self.var_port.get().strip(),
            "user": self.var_user.get().strip(),
            "auth_method": auth if auth in (AUTH_KEY, AUTH_PASSWORD) else AUTH_KEY,
            "key_path": key_out,
            "panels": self.panels,
            "current": cur,
        }

    def _write_config(self, data):
        # 兜底: 任何情况下都不允许明文口令字段落盘
        for bad in ("password", "passwd", "pwd"):
            data.pop(bad, None)
        # 密码: 有值 -> DPAPI 加密成 Base64 密文写 password_enc; 空 -> 不写该字段(=删除)
        pwd = self.var_pwd.get()
        if pwd:
            enc = dpapi_encrypt(pwd)
            if enc:
                data["password_enc"] = enc
            else:
                data.pop("password_enc", None)
                self.log("警告: 密码加密失败(Windows DPAPI 不可用), 本次未保存密码, "
                         "重启后需要重新输入")
        else:
            data.pop("password_enc", None)
        try:
            # 原子写: 先写同目录临时文件 + fsync, 再 os.replace 覆盖,
            # 避免写到一半断电/被杀进程导致 config.json 被截断损坏。
            atomic_write_text(CONFIG_PATH,
                              json.dumps(data, ensure_ascii=False, indent=4))
        except Exception as e:
            self.log(f"保存配置失败: {e}")
            self._notify("配置保存失败", str(e),
                         hints=["确认程序目录可写(未被安全软件锁定)", "确认磁盘空间充足",
                                "若程序放在只读目录, 请整体移动到可写目录后重试"])
            return False
        return True

    def save_config(self, silent=False):
        """把界面当前填写的内容保存到 config.json; silent=True 时不写提示日志之外的反馈"""
        pwd_filled = bool(self.var_pwd.get())
        if not self._write_config(self._collect_config()):
            return
        self._mark_clean()
        self.log(f"配置已保存到 {CONFIG_PATH}")
        if pwd_filled:
            self.log("密码已用 Windows DPAPI 加密后保存(仅当前 Windows 用户可解密)")
        else:
            self.log("密码框为空, 已清除配置中的密码密文(password_enc)")
        if not silent:
            # 保存成功不需要阻断式弹窗: 日志已给出结论, 弹窗反而打断连续操作
            self.log("提示: 密文仅能被当前 Windows 用户在本机解密, 拷到其它电脑/用户下会自动失效需重输密码")

    def _resolve_panel_name(self, text):
        """把下拉显示文本解析回面板名"""
        text = (text or "").strip()
        for p in self.panels:
            name = str(p.get("name", "")).strip()
            if text == name or text.startswith(name + " ("):
                return name
        return text

    def _refresh_panel_values(self, cur_name=None):
        """刷新下拉选项: 完全由 config.json 的面板 name 驱动, 不拼端口"""
        labels = [str(p.get("name", f"面板{i+1}")).strip() for i, p in enumerate(self.panels)]
        self.panel_names = labels
        self.cmb_panel["values"] = labels
        if cur_name:
            self.var_panel.set(cur_name)

    def _on_panel_change(self, event=None):
        """切换面板: 先把当前端口存回旧面板, 再加载新面板端口并刷新下拉显示。

        v2.3: 若切换前存在未保存的编辑, 用日志给出非阻塞提示(说明改动去向),
        不弹窗打断。旧面板的端口值仍会先存回内存, 因此不会真的丢失。
        """
        if not self.panels:
            return
        old_cur = getattr(self, "_last_panel", None)
        new_name = self._resolve_panel_name(self.var_panel.get())
        if old_cur and old_cur != new_name:
            for p in self.panels:
                if str(p.get("name", "")).strip() == old_cur:
                    p["local_port"] = self.var_lport.get().strip()
                    p["remote_port"] = self.var_rport.get().strip()
                    break
        if old_cur and old_cur != new_name and self._dirty:
            self.log(f"注意: 面板『{old_cur}』的修改尚未写入 config.json, "
                     f"已暂存到内存(切回该面板可见); 点『保存配置』即可持久化")
        self._last_panel = new_name
        prev = self._suppress_dirty
        self._suppress_dirty = True     # 程序回填新面板端口, 不算用户编辑
        try:
            for p in self.panels:
                if str(p.get("name", "")).strip() == new_name:
                    self.var_lport.set(str(p.get("local_port", "")).strip())
                    self.var_rport.set(str(p.get("remote_port", "")).strip())
                    break
            self._refresh_panel_values(new_name)
        finally:
            self._suppress_dirty = prev
        self.log(f"已切换到面板: {new_name}")

    def _auto_save(self):
        """启动隧道前静默保存当前配置, 防止修改后未点保存导致丢失"""
        if self._write_config(self._collect_config()):
            self._mark_clean()
            self.log("已自动保存当前配置")
        else:
            self.log("自动保存配置失败")

    def _on_auth_change(self):
        """认证方式切换: 密钥认证填密钥, 密码认证填密码(二选一)"""
        is_key = self.var_auth.get() != AUTH_PASSWORD
        self.ent_key.config(state="normal" if is_key else "disabled")
        self.ent_pwd.config(state="disabled" if is_key else "normal")

    # ---------- 工具方法 ----------
    def log(self, msg):
        """写日志: 主线程直接写, 后台线程经队列转到主线程"""
        if threading.current_thread() is threading.main_thread():
            self._log_now(msg)
        else:
            self._post("log", msg)

    def _log_now(self, msg):
        """追加一行日志(带时间戳)。

        - 自动滚到最新行: 仅在"跟随模式"下滚动, 用户上滚看历史时不强行拉回
        - 行数上限 LOG_MAX_LINES: 超出后从头部裁剪, 长时间运行不会内存膨胀
        - 普通日志 append 到末尾, 避免每次插入都触发全文重排
        """
        box = self.log_box
        box.config(state="normal")
        try:
            box.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
            # 裁剪: 一次裁掉超出量的 10%(至少 50 行), 把 O(n) 操作摊薄
            total = int(box.index("end-1c").split(".")[0])
            if total > LOG_MAX_LINES:
                drop = max(50, (total - LOG_MAX_LINES) // 10 + 1)
                box.delete("1.0", f"{drop + 1}.0")
        finally:
            box.config(state="disabled")
        # 只有处于跟随模式(或接近底部)才自动滚动, 用户手工上滚查看历史时保持原位
        if self._log_follow and box.yview()[1] >= 0.999:
            box.see("end")

    def _scroll_log_to_end(self):
        """强制滚到最新一行(启动收尾/点击"滚到底"按钮时用)。"""
        try:
            self.log_box.see("end")
            self._log_follow = True
        except Exception:
            pass

    def _on_log_scroll(self, event=None):
        """用户滚动日志: 在底部则恢复跟随, 离开底部则暂停跟随(不打断查阅)。"""
        try:
            at_bottom = self.log_box.yview()[1] >= 0.999
            self._log_follow = at_bottom
        except Exception:
            pass

    def _build_log_menu(self):
        """日志区右键菜单 + 快捷键: 复制选中 / 全选 / 清空 / 滚到底。

        日志框常态 disabled 仍可选中文本, 但默认无右键菜单, 这里补上,
        并在窗口级绑定 Ctrl+C / Ctrl+A, 焦点不在日志框时也能复制。
        """
        m = tk.Menu(self.log_box, tearoff=0, bg="#1e1e2e", fg="#cdd6f4",
                    activebackground="#585b70", activeforeground="#ffffff")
        m.add_command(label="复制选中  Ctrl+C", command=self._copy_log_selection)
        m.add_command(label="全选  Ctrl+A", command=self._select_all_log)
        m.add_separator()
        m.add_command(label="清空日志", command=self._clear_log)
        m.add_command(label="滚动到最新", command=self._scroll_log_to_end)
        self._log_menu = m

        def popup(event):
            # 先让 Text 完成光标定位, 再弹菜单, 保证"从光标处复制"符合直觉
            try:
                self.log_box.mark_set("insert", f"@{event.x},{event.y}")
            except Exception:
                pass
            try:
                m.tk_popup(event.x_root, event.y_root)
            finally:
                m.grab_release()
            return "break"

        self.log_box.bind("<Button-3>", popup)
        self.log_box.bind("<Control-c>", lambda e: self._copy_log_selection())
        self.log_box.bind("<Control-a>", lambda e: self._select_all_log())
        self.log_box.bind("<MouseWheel>", self._on_log_scroll)
        self.log_box.bind("<Button-4>", self._on_log_scroll)
        self.log_box.bind("<Button-5>", self._on_log_scroll)
        # 窗口级兜底: 焦点在任意控件上时 Ctrl+C 也能复制日志选中内容
        self.root.bind_all("<Control-Shift-c>", lambda e: self._select_all_log())

    def _copy_log_selection(self):
        """复制日志区选中文本; 未选中时给出非阻塞提示而不是静默无反应。"""
        try:
            text = self.log_box.get("sel.first", "sel.last")
        except Exception:
            self._flash_status("请先用鼠标选中要复制的日志")
            return
        if not text:
            self._flash_status("请先用鼠标选中要复制的日志")
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self._flash_status(f"已复制 {len(text)} 个字符")
        except Exception as e:
            self._flash_status(f"复制失败: {e}")
        return "break"

    def _select_all_log(self):
        try:
            self.log_box.tag_add("sel", "1.0", "end-1c")
            self._flash_status("已全选日志, 按 Ctrl+C 复制")
        except Exception:
            pass
        return "break"

    def _clear_log(self):
        try:
            self.log_box.config(state="normal")
            self.log_box.delete("1.0", "end")
            self.log_box.config(state="disabled")
            self.log("日志已清空")
        except Exception:
            pass

    def _flash_status(self, hint):
        """轻量反馈: 追加一行"提示:"日志。

        不新建控件、不改主界面布局尺寸; 同一提示 1.5s 内重复触发只记一次,
        避免连点按钮时把日志刷满。
        """
        now = time.perf_counter()
        if hint == getattr(self, "_last_hint", None) and \
                now - getattr(self, "_last_hint_ts", 0.0) < 1.5:
            return
        self._last_hint = hint
        self._last_hint_ts = now
        self._log_now(f"提示: {hint}")

    # ---------- 状态机(四态 + 停止) ----------
    def _set_state(self, state):
        """切状态: 主线程直接生效, 后台线程转主线程(避免跨线程操作控件)"""
        if threading.current_thread() is threading.main_thread():
            self._set_state_now(state)
        else:
            self._post("state", state)

    def _set_state_now(self, state):
        """状态文案 + 颜色 + 按钮启用矩阵在同一处更新, 保证三者永远一致。"""
        self._state = state
        text, color = STATE_STYLE.get(state, STATE_STYLE[STATE_IDLE])
        try:
            self.status.config(text=text, fg=color)
        except Exception:
            return
        self._refresh_buttons()

    def _refresh_buttons(self):
        """按 (状态, 忙碌, 脏) 统一刷新按钮可点性, 避免各处散落的 config(state=...)。

        脏标记只影响『保存配置』的底色(有未保存改动时高亮成黄色), 不改按钮尺寸与布局。
        """
        if not hasattr(self, "btn_start"):
            return
        static = self._state in (STATE_STARTING, STATE_RUNNING)
        busy = self._busy
        try:
            self.btn_start.config(state="disabled" if (static or busy) else "normal")
            self.btn_stop.config(state="normal" if static and not busy else "disabled")
            self.btn_open.config(state="normal" if self._state == STATE_RUNNING else "disabled")
            self.btn_save.config(state="disabled" if busy else "normal",
                                 bg=BTN_DIRTY if self._dirty else "#cba6f7")
            self.btn_reload.config(state="disabled" if busy else "normal")
        except Exception:
            pass

    def _enter_busy(self, hint):
        """进入忙碌态: 立即禁用按钮 + 状态行提示, 让点击立刻有反馈。"""
        self._busy = True
        self._flash_status(hint)
        self._refresh_buttons()

    def _leave_busy(self):
        self._busy = False
        self._refresh_buttons()

    # ---------- 未保存修改追踪 ----------
    def _on_var_edited(self, *_args):
        """任一输入项被写入时调用; 程序自身回填期间(_suppress_dirty)不计脏。"""
        if self._suppress_dirty:
            return
        if not self._dirty:
            self._dirty = True
            self._refresh_buttons()

    def _mark_clean(self):
        self._dirty = False
        self._refresh_buttons()

    @staticmethod
    def _parse_port(text):
        """端口字符串 -> int; 非法(空/非数字/越界)返回 None, 绝不抛异常"""
        try:
            port = int(str(text).strip())
        except (TypeError, ValueError):
            return None
        if 1 <= port <= 65535:
            return port
        return None

    def port_open(self, host="127.0.0.1", port=None, timeout=1.0):
        port = self._parse_port(port)
        if port is None:
            return False
        try:
            s = socket.create_connection((host, port), timeout=timeout)
            s.close()
            return True
        except OSError:
            return False

    # ---------- 密码认证辅助 ----------
    def _prepare_askpass(self):
        """生成 askpass 辅助脚本(不含密码), 返回脚本路径; 失败返回 None"""
        self._cleanup_askpass()
        try:
            d = tempfile.mkdtemp(prefix="sgtunnel_askpass_")
            path = os.path.join(d, "askpass.cmd")
            with open(path, "w", encoding="utf-8", newline="\r\n") as f:
                f.write(ASKPASS_SCRIPT)
            self._askpass_dir = d
            self._askpass_path = path
            return path
        except Exception as e:
            self.log(f"创建密码辅助脚本失败: {e}")
            return None

    def _cleanup_askpass(self):
        d = getattr(self, "_askpass_dir", None)
        self._askpass_dir = None
        self._askpass_path = None
        if d and os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)

    # ---------- 动作 ----------
    def _fix_key_permission(self, key):
        """收紧密钥文件 ACL: OpenSSH 拒绝权限过宽的私钥, 文件拷贝后常触发 Bad permissions"""
        try:
            user = os.getenv("USERNAME") or "Administrator"
            subprocess.run(
                ["icacls", key, "/inheritance:r", "/grant:r", f"{user}:(R)"],
                capture_output=True, text=True, timeout=10,
            )
            self.log("已自动修复密钥文件权限")
        except Exception as e:
            self.log(f"修复密钥权限失败(可手动执行 icacls): {e}")

    def _suggest_free_port(self, base, span=30):
        """端口被占时给出一个真实可用的替代端口(只在后台线程调用, 不阻塞主线程)。

        v2.3 体验优化: 旧文案硬编码"如 18306", 用户照抄仍会撞端口; 这里顺次探测
        base+1 .. base+span, 返回第一个探测不通(= 空闲)的端口, 全被占则返回 None。
        """
        try:
            for offset in range(1, span + 1):
                cand = base + offset
                if cand > 65535:
                    break
                if not self.port_open(port=cand):
                    return cand
        except Exception:
            pass
        return None

    def build_cmd(self):
        """按认证方式拼 ssh 命令。密码不进命令行, 只经环境变量交给 askpass 辅助程序。"""
        # 优先取 start_tunnel 已校验过的快照; 未启动时回退到界面输入值(便于单测/预检调用,
        # 且保证任何入口调用都不会因缺属性而 AttributeError)
        lport = getattr(self, "_lport", "") or self.var_lport.get().strip()
        rport = getattr(self, "_rport", "") or self.var_rport.get().strip()
        sshport = getattr(self, "_sshport", "") or self.var_port.get().strip()
        user = getattr(self, "_user", "") or self.var_user.get().strip()
        server = getattr(self, "_server", "") or self.var_ip.get().strip()
        key = getattr(self, "_key", "") or self.var_key.get().strip()
        auth = getattr(self, "_auth", "")
        if auth not in (AUTH_KEY, AUTH_PASSWORD):
            auth = self.var_auth.get()
        if auth not in (AUTH_KEY, AUTH_PASSWORD):
            auth = AUTH_KEY
        cmd = ["ssh", "-N",
               "-L", f"{lport}:{REMOTE_HOST}:{rport}",
               "-p", sshport,
               "-o", "StrictHostKeyChecking=no",
               "-o", "ServerAliveInterval=30",
               "-o", "ExitOnForwardFailure=yes"]
        if auth == AUTH_PASSWORD:
            cmd += ["-o", "BatchMode=no",
                    "-o", "PubkeyAuthentication=no",
                    "-o", "PreferredAuthentications=password,keyboard-interactive",
                    "-o", "NumberOfPasswordPrompts=1"]
        else:
            cmd += ["-i", key]
        cmd.append(f"{user}@{server}")
        return cmd

    def _validate(self):
        """启动前校验, 返回错误列表(空列表=通过)"""
        errs = []
        # 文案规范(V2.3 报错优化): 每条错误都要说清"哪一项"+"现在填的是什么"+"正确该填什么",
        # 让不看手册的人也能照着自己改; 不在文案里甩技术黑话。
        if not self._server:
            errs.append("『服务器 IP』没填: 要填面板所在服务器的地址, 形如 1.2.3.4 或 panel.xxx.com, "
                        "不要带 http:// 和空格")
        if not self._user:
            errs.append("『用户名』没填: 填登录服务器用的账号名(常见为 root), 不是密码")
        if self._parse_port(self._sshport) is None:
            errs.append(f"『SSH 端口』填得不对: 现在是 '{self._sshport}', "
                        "只能填 1-65535 的整数, 通常填 22")
        if self._parse_port(self._lport) is None:
            errs.append(f"『本地端口』填得不对: 现在是 '{self._lport}', "
                        "只能填 1-65535 的整数, 建议 1025 以上(避开系统占用端口)")
        if self._parse_port(self._rport) is None:
            errs.append(f"『远程端口』填得不对: 现在是 '{self._rport}', "
                        "只能填 1-65535 的整数, 且要与服务器上面板实际监听的端口一致")
        if self._auth == AUTH_PASSWORD:
            if not self._password:
                errs.append("『密码』没填: 当前选的是密码认证, 必须输入服务器的登录密码")
        else:
            if not self._key:
                errs.append("『密钥文件』没选: 当前选的是密钥认证, 需要选中私钥文件(如 server_key)")
            elif not os.path.exists(self._key):
                errs.append(f"『密钥文件』指向的文件不存在: {self._key} "
                            "(文件被移动/改名了, 重新选一次即可)")
        return errs

    def start_tunnel(self):
        """点击『启动隧道』: 立刻进入忙碌态(按钮禁用 + 状态变黄), 校验与联网动作全在后台线程。

        v2.3 体验优化: 校验/端口预检/密钥权限修复/拉起进程都不再占用主线程,
        按钮点击后界面立刻有反应, 连点也不会重复起进程(按钮已禁用 + _busy 双保险)。
        """
        if self.running or self._busy:
            self._flash_status("隧道已在运行或正在启动, 请稍候")
            return
        # 主线程读取界面值存为普通变量, 供后台线程与命令构造安全使用
        self._lport = self.var_lport.get().strip()
        self._rport = self.var_rport.get().strip()
        self._server = self.var_ip.get().strip()
        self._sshport = self.var_port.get().strip()
        self._user = self.var_user.get().strip()
        self._key = self.var_key.get().strip()
        self._auth = self.var_auth.get()
        if self._auth not in (AUTH_KEY, AUTH_PASSWORD):
            self._auth = AUTH_KEY
        self._password = self.var_pwd.get()   # 明文只在内存: 不进命令行, 落盘仅 DPAPI 密文

        # 1) 同步做零 IO 的字段校验(纯字符串判断, 微秒级)
        errs = self._validate()
        if errs:
            self._password = ""
            self._set_state(STATE_ERROR)
            self._notify("配置不完整, 未启动隧道", "; ".join(errs),
                         hints=self._fix_hints(errs))
            return

        # 2) 进入忙碌态并拉起后台启动流程
        self._enter_busy("正在启动隧道, 请稍候...")
        self._set_state(STATE_STARTING)
        threading.Thread(target=self._start_worker, daemon=True).start()

    @staticmethod
    def _fix_hints(errs):
        """把校验错误翻译成"照着点就能改"的动作清单(错误提示可操作化)。"""
        hints = []
        joined = " ".join(errs)
        if "服务器 IP" in joined:
            hints.append("『服务器 IP』填纯地址: 如 1.2.3.4 或 panel.xxx.com, 别带 http:// 和空格")
        if "用户名" in joined:
            hints.append("『用户名』填 SSH 登录账号(如 root), 不是你电脑的开机账号, 也不是密码")
        if "SSH 端口" in joined:
            hints.append("『SSH 端口』一般就是 22; 如果服务器改过 sshd 的 Port, 就填改后的那个数字")
        if "本地端口" in joined:
            hints.append("『本地端口』是你本机访问用的入口, 填 1025 以上的数字最稳妥; "
                         "程序会自动帮你找没被占用的端口")
        if "远程端口" in joined:
            hints.append("『远程端口』必须等于服务器上面板真正监听的端口, "
                         "可在服务器执行 ss -lntp 查看")
        if "『密码』" in joined:
            hints.append("当前是密码认证: 在『密码』框输入服务器登录密码"
                         "(只在内存使用, 保存时只存 Windows 加密后的密文)")
        if "『密钥文件』" in joined:
            hints.append("当前是密钥认证: 点『密钥文件』右侧按钮选中 server_key 这个私钥文件"
                         "(不是 server_key.pub), 且公钥要已加到服务器 ~/.ssh/authorized_keys")
        if not hints:
            hints.append("按日志提示修正配置后, 重新点『启动隧道』")
        return hints

    def _start_worker(self):
        """后台线程: 端口预检 → askpass/密钥权限 → 拉起 ssh 进程, 结果经队列回主线程。"""
        try:
            lport = self._parse_port(self._lport)
            if self.port_open(port=lport):
                alt = self._suggest_free_port(lport)
                tip = f", 建议改用 {alt}" if alt else ""
                self._post("start_error", "port_busy",
                           f"『本地端口』{lport} 已经被别的程序占用了{tip}, 隧道没能启动")
                return
            env = None
            if self._auth == AUTH_PASSWORD:
                askpass = self._prepare_askpass()
                if not askpass:
                    self._post("start_error", "askpass",
                               "没法在临时目录写密码应答脚本(目录不可写), 密码认证启动不了")
                    return
                # 密码只经子进程环境变量传递: 不出现在命令行, 不写文件
                env = dict(os.environ)
                env.update({
                    "SSH_ASKPASS": askpass,
                    "SSH_ASKPASS_REQUIRE": "force",
                    "DISPLAY": os.environ.get("DISPLAY", "localhost:0"),
                    "SG_TUNNEL_PW": self._password,
                })
                self._post("log", "认证方式: 密码(明文仅内存与子进程环境变量, 不进命令行; 落盘只存 DPAPI 密文)")
            else:
                self._fix_key_permission(self._key)
                self._post("log", "认证方式: 密钥")

            cmd = self.build_cmd()
            self._post("log", "启动隧道: " + " ".join(cmd))
            kwargs = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
            }
            if self._auth == AUTH_PASSWORD:
                kwargs["stdin"] = subprocess.DEVNULL
                kwargs["env"] = env
            self.proc = subprocess.Popen(cmd, **kwargs)
        except FileNotFoundError:
            self._post("start_error", "no_ssh",
                       "这台电脑上没有 ssh.exe(Windows 还没装 OpenSSH 客户端), 隧道起不来")
            return
        except Exception as e:
            self._post("start_error", "spawn", f"ssh 进程拉不起来: {e}")
            return
        self._post("started")

    def _on_started(self):
        """主线程: ssh 进程已成功拉起, 进入启动中状态并开始监听。"""
        self.running = True
        self._leave_busy()
        self._set_state(STATE_STARTING)
        # 启动前静默保存当前配置(密码以 DPAPI 密文落盘)
        self._auto_save()
        threading.Thread(target=self._monitor, daemon=True).start()

    def _on_start_failed(self, reason, detail):
        """主线程: 启动前/启动中失败, 统一按错误态收尾并给出可操作提示。"""
        self.running = False
        self.proc = None
        self._password = ""
        self._cleanup_askpass()
        self._leave_busy()
        self._set_state(STATE_ERROR)
        hints = {
            "port_busy": ["把『本地端口』改成上一条日志里给出的可用端口, 或先关掉占用它的程序再重试",
                          "想知道是谁占用的: Win+R 输入 cmd, 执行 netstat -ano | findstr :端口号, "
                          "再用任务管理器按 PID 结束"],
            "askpass": ["把整个程序目录挪到可写位置(如 D:/Tools/sgtunnel), 别放在 C:/Program Files 下运行",
                        "或在『认证方式』里改成密钥认证后重新启动"],
            "no_ssh": ["Win+R 输入 optionalfeatures, 勾选『OpenSSH 客户端』确定, 装完重启本程序",
                       "或用管理员 PowerShell 执行: Add-WindowsCapability -Online "
                       "-Name OpenSSH.Client~~~~0.0.1.0"],
            "spawn": ["把程序目录加进杀毒/安全软件白名单后重试",
                      "打开 cmd 执行 ssh -V, 能版本号说明 ssh 正常; 报错则先装 OpenSSH 客户端"],
        }.get(reason, ["看上方日志里 ssh 原文的『含义』与『处理建议』两行, 按建议改完再点『启动隧道』"])
        self._notify("隧道启动失败", detail, hints=hints)

    def _notify(self, title, detail, hints=None):
        """统一错误/警告提示: 只写日志(结论 + 处理建议), 不弹阻塞式 messagebox。

        状态栏切换由调用方按语境决定(启动失败切 error, 打开面板失败保持原状态),
        避免"提示一次就把绿状态染红"。
        每条提示固定三段——结论 / 原因或细节 / 怎么修, 让用户不用猜该动哪里。
        """
        try:
            self.log(f"【{title}】{detail}")
            for h in (hints or []):
                self.log(f"  → 处理建议: {h}")
        except Exception:
            pass

    @staticmethod
    def _explain_ssh_line(line):
        """把 ssh 英文报错翻成(含义, 处理建议); 没命中返回 None —— 不认识就不瞎解释。"""
        low = line.lower()
        for pat, meaning, fix in SSH_LINE_HINTS:
            if pat in low:
                return meaning, fix
        return None

    def _monitor(self):
        """后台线程: 等待端口就绪 + 持续转发 ssh 输出。只经队列与界面交互。"""
        proc = self.proc
        if proc is None:
            return
        lport = self._parse_port(self._lport)
        ready = False
        # 阶段1: 等待本地端口就绪(最多 15 秒)
        for _ in range(30):
            if not self.running or proc.poll() is not None:
                self._post("exited")
                return
            if lport is not None and self.port_open(port=lport):
                ready = True
                break
            time.sleep(0.5)
        if not self.running:
            return
        if ready:
            self._post("state", STATE_RUNNING)
            self._post("log", "本地端口已就绪, 隧道正常运行(浏览器访问 127.0.0.1 对应端口即可)")
        elif lport is None:
            self._post("log", "本地端口无效, 没法检测就绪状态(回到配置区检查『本地端口』)")
        else:
            self._post("log", f"本地端口 {lport} 在 15 秒内还没通, 隧道可能没建成功: "
                              "继续看下面的 ssh 输出, 失败原因通常就在那几行里")
        # 阶段2: 逐行转发 ssh 输出; 进程退出时 stdout 关闭 -> 循环自然结束。
        # 每行英文报错后面紧跟一段人话翻译(含义 + 处理建议), 原文保留便于搜索与反馈。
        try:
            for line in iter(proc.stdout.readline, ""):
                if not self.running:
                    break
                line = line.strip()
                if line:
                    self._post("log", "ssh: " + line)
                    exp = self._explain_ssh_line(line)
                    if exp:
                        self._post("log", f"  → 含义: {exp[0]}")
                        self._post("log", f"  → 处理建议: {exp[1]}")
        except Exception as e:
            self._post("log", f"读取 ssh 输出异常: {e}")
        if self.running:
            self._post("exited")

    def _proc_exit(self):
        """主线程: ssh 进程已退出"""
        if not self.running:
            return
        self.running = False
        self._leave_busy()
        self._set_state(STATE_STOPPED)
        code = self.proc.returncode if self.proc else None
        self.log(f"SSH 进程已退出(退出码 {code})")
        # 退出码翻译: 结论 + 下一步动作, 不让用户对着一个数字猜
        concl, action = SSH_EXIT_HINTS.get(
            code,
            (f"ssh 异常退出(退出码 {code})",
             "看上方 ssh 原文的『含义』与『处理建议』两行; 原文为空多为网络被中断, 直接重试一次"))
        if code not in (0, None):
            self._set_state(STATE_ERROR)
        self.log(f"  → 含义: {concl}")
        self.log(f"  → 处理建议: {action}")
        if getattr(self, "_auth", AUTH_KEY) == AUTH_PASSWORD:
            self.log("提示: 若确认密码填对了还是被拒, 在 cmd 执行 ssh -V 看系统 OpenSSH 版本, "
                     "过老的版本可能不支持密码自动应答")
        self._password = ""
        self._cleanup_askpass()

    def stop_tunnel(self):
        """点击『停止隧道』: 立即进入忙碌态, terminate/wait 放到后台线程, 界面不卡。"""
        if self._busy:
            return
        if not self.proc and not self.running:
            self._flash_status("当前没有运行中的隧道, 无需停止")
            self._set_state(STATE_STOPPED)
            return
        self._enter_busy("正在停止隧道, 请稍候...")
        self.running = False           # 先置位: 让 _monitor 停止转发并避免 _proc_exit 重复收尾
        threading.Thread(target=self._stop_worker, daemon=True).start()

    def _stop_worker(self):
        proc = self.proc
        if proc:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._post("log", "ssh 未在 5 秒内退出, 强制结束")
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self._post("log", "⚠ 强制结束超时, 请在任务管理器中结束残留的 ssh.exe")
                else:
                    pass
            except Exception as e:
                self._post("log", f"停止时发生异常: {e}")
        self.proc = None
        self._post("stopped")

    def _on_stopped(self):
        self.running = False
        self._leave_busy()
        self._set_state(STATE_STOPPED)
        self._password = ""
        self._cleanup_askpass()
        self.log("隧道已停止")

    def open_panel(self):
        lport = self._parse_port(self.var_lport.get().strip() or getattr(self, "_lport", ""))
        if lport is None:
            self._flash_status("『本地端口』不对, 先在配置区填 1-65535 的数字")
            self._notify("打开面板失败", f"『本地端口』现在是 '{self.var_lport.get()}', 不是 1-65535 的整数",
                         hints=["在『本地端口』填 1025 以上的数字(比如 8443)后, 再点『打开面板』"])
            return
        if not self.port_open(port=lport):
            self._flash_status("隧道还没连上, 请先点『启动隧道』")
            self._notify("打开面板失败", f"本地端口 {lport} 现在没有程序在听, 说明隧道还没连上",
                         hints=["先点『启动隧道』, 等状态变成『隧道已连接』再点『打开面板』",
                                "已经启动却仍打不开: 看日志里 ssh 原文下面的『含义』行, "
                                "常见是远程端口不通或登录被拒"])
            return
        url = f"https://127.0.0.1:{lport}"
        webbrowser.open(url)
        self.log(f"已在浏览器打开面板: {url}")

    # ---------- 轮询 ----------
    def _tick(self):
        """自适应轮询: 空闲时 5s 一次且不做任何 IO; 运行中 2s 一次, 端口探测放后台线程。

        原实现固定 2s 在主线程做 socket 探测(最长阻塞 1s), 会让界面周期性卡顿,
        并在空闲时也无意义地反复空转。
        """
        self._tick_id = None
        if self.running:
            # 探测交给后台线程, 主线程只负责按结果刷状态
            if not self._probe_busy:
                self._probe_busy = True
                threading.Thread(target=self._probe_worker, daemon=True).start()
            interval = TICK_RUN_MS
        else:
            interval = TICK_IDLE_MS
        self._tick_id = self.root.after(interval, self._tick)

    def _probe_worker(self):
        """后台端口探测; 无论成功失败都经队列回一次结果, 保证 _probe_busy 必定释放。"""
        ok = False
        try:
            lport = self._parse_port(getattr(self, "_lport", ""))
            ok = self.port_open(port=lport) if lport is not None else False
        except Exception:
            ok = False
        finally:
            self._post("probe", ok)

    def _on_probe(self, ok):
        """后台探测结果回到主线程后再改状态(绝不在后台线程碰控件)。"""
        self._probe_busy = False
        if not self.running:
            return
        proc = self.proc
        if proc is not None and proc.poll() is not None:
            self._proc_exit()
            return
        if ok:
            if self._state != STATE_RUNNING:
                self._set_state(STATE_RUNNING)
        elif self._state != STATE_STARTING:
            self._set_state(STATE_STARTING)

    # ---------- 关闭 ----------
    def _on_close(self):
        try:
            if self._dirty:
                # 非阻塞提示 + 自动落盘: 关闭前不弹窗打断, 但也不让最后的手工改动静默丢失
                self.log("检测到未保存的修改, 关闭前自动写入 config.json")
                try:
                    self._write_config(self._collect_config())
                except Exception as e:
                    self.log(f"关闭前自动保存失败(配置保持原样): {e}")
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
                except Exception:
                    pass
        finally:
            self._password = ""
            self._cleanup_askpass()
            self.root.destroy()


# ===== ssh 英文报错翻译表(V2.3 报错文案优化轮) =====
# ssh 自己吐出来的是英文原文, 小白看不懂也不会改。这里按"匹配片段 -> 人话含义 -> 照做就行"三段式
# 做一次翻译, 日志里先出原文再出翻译两行, 原文保留便于搜索与反馈。
# 匹配用小写包含判断, 顺序即优先级: 越具体的放越前面(如 Permission denied (publickey) 先于 Permission denied)。
SSH_LINE_HINTS = (
    ("host key verification failed",
     "服务器的身份指纹和上次记录的不一致(常见于服务器重装系统或换了 IP)",
     "打开 C:\\Users\\你的用户名\\.ssh\\known_hosts, 删掉与这台服务器 IP 对应的那一行, 再点『启动隧道』"),
    ("remote host identification has changed",
     "服务器指纹变了(服务器重装过系统或被换了机器), 被 ssh 主动拦下",
     "同上: 删掉 known_hosts 里与这台服务器 IP 对应的旧记录后重试"),
    ("bind [",
     "本地端口绑不上: ssh 在本机占不到这个端口(被别的程序占用, 或被系统保留)",
     "把『本地端口』改成 1025 以上的其它数字(日志里会给建议值), 或结束占用它的程序"),
    ("permission denied (publickey,password)",
     "服务器拒绝了这次登录: 密码不对, 或者这把密钥没被服务器接受",
     "密码认证就重新输入密码; 密钥认证要确认 server_key.pub 已加到服务器 ~/.ssh/authorized_keys"),
    ("permission denied (publickey",
     "服务器不认这把密钥: 公钥没上传到服务器, 或『密钥文件』选错了",
     "把 server_key.pub 的内容追加到服务器 ~/.ssh/authorized_keys, 并确认『密钥文件』选的是 server_key(不是 .pub 那个)"),
    ("permission denied",
     "登录被服务器拒绝: 账号或密码/密钥不正确, 或该账号被禁止 SSH 登录",
     "先核对『用户名』和『密码』; 密钥认证确认公钥已上传; 仍不行看服务器 /var/log/auth.log"),
    ("administratively prohibited",
     "服务器禁止了端口转发: sshd_config 里 AllowTcpForwarding 被关掉了",
     "在服务器上把 /etc/ssh/sshd_config 的 AllowTcpForwarding 改成 yes, 再重启 sshd"),
    ("open failed",
     "远程端口上没有服务在监听, 转发通道打不开(面板没跑, 或『远程端口』填错)",
     "在服务器执行 curl -k https://127.0.0.1:远程端口 验证面板是否活着, 再核对『远程端口』"),
    ("connection refused",
     "服务器的这个端口上没有 SSH 服务在听: sshd 没启动, 或『SSH 端口』填的不是 SSH 端口",
     "登录服务器执行 systemctl status sshd 看服务是否在跑, 并确认端口与 sshd 配置里的 Port 一致"),
    ("connection timed out",
     "连不上服务器: 地址写错、服务器没开机, 或云厂商安全组没放行这个端口",
     "先 ping 一下地址, 再到云控制台安全组里放行该 SSH 端口后重试"),
    ("operation timed out",
     "连接超时: 网络不通或被防火墙静默丢弃",
     "检查本机网络与服务器安全组, 确认该端口对外放行"),
    ("no route to host",
     "这台电脑到服务器的网络走不通(断网、IP 网段不对或被防火墙拦)",
     "确认本机联网正常, 且『服务器 IP』与你在同一可访问网段"),
    ("connection reset by",
     "服务器把连接掐断了: 多为防爆破策略(fail2ban)把你这个 IP 临时拉黑",
     "等 5~10 分钟再试; 确认服务器上没有把你当前公网 IP 加入黑名单"),
    ("kex_exchange_identification",
     "端口连上了但对面不像 SSH 服务(常见是『SSH 端口』填成了面板端口, 或被中间设备接管)",
     "核对『SSH 端口』是不是 sshd 真正监听的端口(一般 22), 不要填面板的端口"),
    ("connection closed by",
     "服务器主动断开连接: 该账号被限制了来源 IP, 或服务器负载过高",
     "确认服务器 sshd 没有限制 AllowUsers/来源 IP; 稍后重试"),
    ("could not resolve hostname",
     "『服务器 IP』里填的地址解析不出来(拼写错误, 或带了 http:// 、空格)",
     "只填纯 IP 或域名, 不要带协议头、端口号、路径"),
    ("address already in use",
     "本地端口已被别的程序占用, 转发通道建不起来",
     "换一个本地端口(日志里会给出探测到的可用端口), 或结束占用该端口的程序"),
    ("unprotected private key file",
     "私钥文件权限太宽松, ssh 出于安全拒绝使用它",
     "程序启动时已自动收紧权限; 若仍报错, 右键私钥→属性→安全, 只保留当前用户的读取权限"),
    ("invalid format",
     "私钥文件格式不对或文件已损坏",
     "确认『密钥文件』选的是私钥原文(以 -----BEGIN OPENSSH PRIVATE KEY----- 开头), 不是 .pub 也不是 .ppk"),
    ("too many authentication failures",
     "连续认证失败次数太多, 被服务器暂时拒绝",
     "等 1~2 分钟再试; 确认『密钥文件』只指向一把正确的私钥"),
    ("no matching host key type found",
     "客户端与服务器没有共同的主机密钥算法(老服务器常见)",
     "在服务器 sshd_config 里加 HostKeyAlgorithms +ssh-rsa 后重启 sshd"),
    ("request failed on channel",
     "服务器没给这个会话分配 shell: 该账号可能被设成不允许登录/只允许转发",
     "确认该账号在服务器 /etc/passwd 里的 shell 是 /bin/bash, 或换一个有登录权限的账号"),
)

# ssh 退出码 -> (一句话结论, 下一步动作)
SSH_EXIT_HINTS = {
    0: ("ssh 正常结束(连接是自己断的或被服务器正常关闭)",
        "若你没点『停止隧道』, 说明服务器那边把会话关了, 重新点『启动隧道』即可"),
    1: ("ssh 报通用错误(参数或本地环境问题居多)",
        "看上方 ssh 原文与『含义』行; 常见为参数不被支持或私钥路径不对"),
    255: ("ssh 自身报错退出(认证失败、网络不通或主机指纹校验未通过)",
          "重点看上方 ssh 原文的『含义』与『处理建议』两行, 按建议改完再启动"),
}

def main():
    root = tk.Tk()
    TunnelApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()



