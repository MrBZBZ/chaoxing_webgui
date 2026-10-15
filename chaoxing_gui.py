#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chaoxing_gui.py — 学习通课程视频自动完成（图形界面）。

零依赖：tkinter 是 Python 自带。双击运行或在命令行 `python chaoxing_gui.py`。
复用 chaoxing_autowatch.py 的全部逻辑，只是把参数和日志搬到窗口里。

界面流程：
  1. 粘贴 cookie（从 Reqable 任一条 chaoxing 请求头里整段复制）和 studentstudy 链接
  2. 点【体检】→ 列出全部视频任务点与完成状态（只读，不发任何上报）
  3. 点【开始】→ 自动逐个视频按真实节奏上报，直到 isPassed=true；随时可【停止】
"""

import contextlib
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaoxing_autowatch as core   # noqa: E402


class QueueWriter:
    """把 print() 输出送进 UI 队列。"""

    def __init__(self, q):
        self.q = q
        self.buf = ''

    def write(self, text):
        self.buf += text
        while '\n' in self.buf:
            line, self.buf = self.buf.split('\n', 1)
            self.q.put(('log', line))

    def flush(self):
        if self.buf:
            self.q.put(('log', self.buf))
            self.buf = ''


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.worker = None
        self.busy = False
        root.title('学习通 · 课程视频自动完成')
        root.geometry('980x720')

        pad = {'padx': 8, 'pady': 4}

        # ---------------- 配置区 ----------------
        cfg = ttk.LabelFrame(root, text='① 参数')
        cfg.pack(fill='x', **pad)

        ttk.Label(cfg, text='Cookie（Reqable 里整段复制）').grid(row=0, column=0, sticky='w', **pad)
        self.cookie_txt = tk.Text(cfg, height=3, wrap='char')
        self.cookie_txt.grid(row=1, column=0, columnspan=6, sticky='we', **pad)
        ttk.Button(cfg, text='从文件载入', command=self.load_cookie).grid(row=2, column=0, sticky='w', **pad)
        ttk.Button(cfg, text='保存到文件', command=self.save_cookie).grid(row=2, column=1, sticky='w', **pad)

        ttk.Label(cfg, text='studentstudy 链接').grid(row=3, column=0, sticky='w', **pad)
        self.url_var = tk.StringVar()
        ttk.Entry(cfg, textvariable=self.url_var).grid(row=3, column=1, columnspan=5, sticky='we', **pad)

        ttk.Label(cfg, text='上报间隔(秒)').grid(row=4, column=0, sticky='w', **pad)
        self.interval_var = tk.StringVar(value='62')
        ttk.Entry(cfg, textvariable=self.interval_var, width=8).grid(row=4, column=1, sticky='w', **pad)

        ttk.Label(cfg, text='速率(1.0=正常,2.0=双倍)').grid(row=4, column=2, sticky='e', **pad)
        self.rate_var = tk.StringVar(value='1.0')
        ttk.Entry(cfg, textvariable=self.rate_var, width=8).grid(row=4, column=3, sticky='w', **pad)

        ttk.Label(cfg, text='限时(分钟,0=不限)').grid(row=4, column=4, sticky='e', **pad)
        self.maxmin_var = tk.StringVar(value='0')
        ttk.Entry(cfg, textvariable=self.maxmin_var, width=8).grid(row=4, column=5, sticky='w', **pad)

        ttk.Label(cfg, text='只做这些节点(可选,逗号分隔)').grid(row=5, column=0, sticky='w', **pad)
        self.nodes_var = tk.StringVar()
        ttk.Entry(cfg, textvariable=self.nodes_var).grid(row=5, column=1, columnspan=5, sticky='we', **pad)
        for c in range(6):
            cfg.columnconfigure(c, weight=1 if c == 1 else 0)

        # ---------------- 操作区 ----------------
        bar = ttk.Frame(root)
        bar.pack(fill='x', **pad)
        self.btn_probe = ttk.Button(bar, text='体检（只读）', command=lambda: self.start(probe=True))
        self.btn_probe.pack(side='left', **pad)
        self.btn_run = ttk.Button(bar, text='开始自动观看', command=lambda: self.start(probe=False))
        self.btn_run.pack(side='left', **pad)
        self.btn_stop = ttk.Button(bar, text='停止', command=self.stop, state='disabled')
        self.btn_stop.pack(side='left', **pad)
        ttk.Button(bar, text='打开说明', command=self.open_readme).pack(side='left', **pad)
        self.status = ttk.Label(bar, text='就绪')
        self.status.pack(side='right', **pad)

        self.prog = ttk.Progressbar(root, mode='indeterminate')
        self.prog.pack(fill='x', **pad)

        # ---------------- 任务表 ----------------
        table_box = ttk.LabelFrame(root, text='② 视频任务点')
        table_box.pack(fill='both', expand=False, **pad)
        cols = ('node', 'title', 'dur', 'state')
        self.tree = ttk.Treeview(table_box, columns=cols, show='headings', height=9)
        for c, t, w in (('node', '节点ID', 110), ('title', '标题', 380),
                        ('dur', '时长(秒)', 90), ('state', '状态', 120)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor='w')
        self.tree.pack(side='left', fill='both', expand=True)
        sb = ttk.Scrollbar(table_box, orient='vertical', command=self.tree.yview)
        sb.pack(side='right', fill='y')
        self.tree.configure(yscrollcommand=sb.set)

        # ---------------- 日志 ----------------
        log_box = ttk.LabelFrame(root, text='③ 运行日志')
        log_box.pack(fill='both', expand=True, **pad)
        self.log = scrolledtext.ScrolledText(log_box, height=14, wrap='word')
        self.log.pack(fill='both', expand=True, **pad)

        self.root.after(120, self.drain)
        self.root.protocol('WM_DELETE_WINDOW', self.on_close)

    # ------------------------------------------------------------------ #
    def logline(self, text):
        self.log.insert('end', text + '\n')
        self.log.see('end')

    def set_busy(self, busy, status=None):
        self.busy = busy
        self.btn_probe.configure(state='disabled' if busy else 'normal')
        self.btn_run.configure(state='disabled' if busy else 'normal')
        self.btn_stop.configure(state='normal' if busy else 'disabled')
        if status:
            self.status.configure(text=status)
        if busy:
            self.prog.start(12)
        else:
            self.prog.stop()

    def drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == 'log':
                    self.logline(payload)
                elif kind == 'status':
                    self.status.configure(text=payload)
                elif kind == 'row':
                    node, title, dur, state = payload
                    self.tree.insert('', 'end', values=(node, title, dur, state))
                elif kind == 'clear':
                    self.tree.delete(*self.tree.get_children())
                elif kind == 'done':
                    self.set_busy(False, payload)
        except queue.Empty:
            pass
        self.root.after(120, self.drain)

    # ------------------------------------------------------------------ #
    def build_argv(self, probe):
        cookie = self.cookie_txt.get('1.0', 'end').strip()
        url = self.url_var.get().strip()
        if not cookie:
            raise ValueError('请先粘贴 cookie')
        if not url:
            raise ValueError('请填写 studentstudy 链接')
        argv = ['--cookie', cookie, '--from-url', url,
                '--interval', self.interval_var.get().strip() or '60',
                '--rate', self.rate_var.get().strip() or '1.0']
        mm = self.maxmin_var.get().strip()
        if mm and float(mm) > 0:
            argv += ['--max-minutes', mm]
        nodes = self.nodes_var.get().strip()
        if nodes:
            argv += ['--nodes', nodes]
        if probe:
            argv += ['--probe']
        return argv, cookie, url

    def start(self, probe):
        if self.busy:
            return
        try:
            argv, cookie, url = self.build_argv(probe)
        except Exception as exc:
            messagebox.showwarning('参数不完整', str(exc))
            return
        self.q.put(('clear', None))
        self.log.delete('1.0', 'end')
        self.set_busy(True, '体检中…' if probe else '运行中…')
        self.worker = threading.Thread(target=self.run_worker, args=(argv, cookie, url, probe),
                                       daemon=True)
        self.worker.start()

    def run_worker(self, argv, cookie, url, probe):
        writer = QueueWriter(self.q)
        try:
            # 先自己扫描一遍，把结构化结果填进表格
            ctx = core.parse_studentstudy_url(url)
            ctx['userid'] = self._cookie_value(cookie, '_uid', 'UID')
            ctx['fid'] = self._cookie_value(cookie, 'fid')
            if not ctx['userid'] or not ctx['fid']:
                raise RuntimeError('cookie 里缺少 _uid / fid，请确认复制完整')
            client = core.HttpClient(cookie)
            self.q.put(('status', '读取节点列表…'))
            nodes = core.list_nodes(client, ctx)
            self.q.put(('log', '共 %d 个节点，开始扫描视频任务点…' % len(nodes)))
            total = pending = 0
            for n in nodes:
                try:
                    vids = core.video_attachments(client, ctx, n['id'])
                except Exception:
                    continue
                for v in vids:
                    total += 1
                    if not v['isPassed']:
                        pending += 1
                    self.q.put(('row', (n['id'], (n['title'] or '')[:40],
                                        v['duration'],
                                        '已完成' if v['isPassed'] else '未完成')))
            self.q.put(('log', '视频任务点共 %d 个，其中未完成 %d 个（合计约 %d 秒）'
                        % (total, pending, 0)))
            if probe:
                self.q.put(('done', '体检完成'))
                return

            self.q.put(('status', '自动观看中…'))
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                try:
                    core.main(argv)
                except SystemExit as exc:
                    self.q.put(('log', '[退出] %s' % (exc,)))
            self.q.put(('done', '结束'))
        except Exception as exc:
            self.q.put(('log', '[错误] %r' % (exc,)))
            self.q.put(('done', '出错'))

    @staticmethod
    def _cookie_value(cookie, *names):
        import re
        for name in names:
            m = re.search(r'(?:^|;\s*)%s=([^;]*)' % re.escape(name), cookie)
            if m:
                return m.group(1)
        return None

    # ------------------------------------------------------------------ #
    def stop(self):
        core.request_stop()
        self.status.configure(text='停止中…（等当前等待结束）')
        self.logline('[已发送停止指令]')

    def load_cookie(self):
        path = filedialog.askopenfilename(title='选择 cookie 文件')
        if not path:
            return
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            self.cookie_txt.delete('1.0', 'end')
            self.cookie_txt.insert('1.0', fh.read().strip())
        self.logline('已载入 cookie: %s' % path)

    def save_cookie(self):
        text = self.cookie_txt.get('1.0', 'end').strip()
        if not text:
            return
        path = filedialog.asksaveasfilename(title='保存 cookie', defaultextension='.txt')
        if not path:
            return
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(text)
        self.logline('已保存 cookie: %s' % path)

    def open_readme(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'README_autowatch.md')
        if os.path.exists(path):
            try:
                os.startfile(path)   # noqa: S606 (Windows)
            except Exception as exc:
                messagebox.showinfo('说明文件', '%s\n(%r)' % (path, exc))
        else:
            messagebox.showinfo('说明文件', '未找到 README_autowatch.md')

    def on_close(self):
        core.request_stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        root.call('tk', 'scaling', 1.25)
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
