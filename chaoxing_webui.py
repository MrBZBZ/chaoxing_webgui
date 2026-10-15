#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chaoxing_webui.py — 学习通课程视频自动完成 · 本机网页版（零依赖）。

相比桌面版(tkinter)：网页版不受“沙箱/后台桌面”限制，浏览器能开就能看到。

功能：
  * 打开页面自动刷新一次状态列表（可在界面里关掉）
  * 勾选任意视频 → 「从选中开始继续」：先重跑它（即使显示已完成），
    再继续把它后面的未完成视频做完
  * 「开始全部未完成」：一次做完所有未完成视频
  * 实时日志 + 进度（第 i / n 个）

启动：
    python chaoxing_webui.py --port 8756
"""

import argparse
import contextlib
import json
import os
import re
import socket
import sys
import time
import threading
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaoxing_autowatch as core   # noqa: E402

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

STATE = {
    'logs': [],
    'tree': [],          # [{node,title,dur,state}] 按课程顺序
    'status': '就绪',
    'busy': False,
    'summary': '',
    'progress': {'done': 0, 'total': 0},
}
LOCK = threading.Lock()
PRESET = {}
WORKER = {'thread': None}

SCAN_COOLDOWN = 20.0          # 秒：非强制刷新在这个时间内直接跳过
LAST_SCAN = {'ts': 0.0}
SCAN_WORKERS = 8              # 并行扫描节点数

PROGRESS_RE = re.compile(r'^\[(\d+)/(\d+)\]\s')


class _Writer:
    def __init__(self, push):
        self.push = push
        self.buf = ''

    def write(self, text):
        self.buf += text
        while '\n' in self.buf:
            line, self.buf = self.buf.split('\n', 1)
            self.push(line)

    def flush(self):
        if self.buf:
            self.push(self.buf)


def push_log(line):
    m = PROGRESS_RE.match(line)
    with LOCK:
        STATE['logs'].append(line)
        if len(STATE['logs']) > 20000:
            del STATE['logs'][:5000]
        if m:
            STATE['progress'] = {'done': int(m.group(1)) - 1,
                                 'total': int(m.group(2))}
        elif '已通过' in line:
            STATE['progress']['done'] = min(
                STATE['progress']['done'] + 1,
                STATE['progress']['total'] or 10 ** 9)


def set_status(text):
    with LOCK:
        STATE['status'] = text


def _cookie_value(cookie, *names):
    for name in names:
        m = re.search(r'(?:^|;\s*)%s=([^;]*)' % re.escape(name), cookie)
        if m:
            return m.group(1)
    return None


def scan(cookie, url):
    """扫描课程 → 填充表格/日志。返回 ctx。"""
    ctx = core.parse_studentstudy_url(url)
    ctx['userid'] = _cookie_value(cookie, '_uid', 'UID')
    ctx['fid'] = _cookie_value(cookie, 'fid')
    if not ctx['userid'] or not ctx['fid']:
        raise RuntimeError('cookie 里缺少 _uid / fid，请确认整段复制')
    client = core.HttpClient(cookie)
    set_status('读取节点列表…')
    nodes = core.list_nodes(client, ctx)
    push_log('共 %d 个节点，开始并行扫描（%d 并发）…' % (len(nodes), SCAN_WORKERS))

    def probe_one(node):
        try:
            return node, core.video_attachments(client, ctx, node['id'])
        except Exception:
            return node, []

    rows = []
    total = pending = pend_secs = 0
    done = 0
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        for n, vids in pool.map(probe_one, nodes):
            done += 1
            if done % 10 == 0 or done == len(nodes):
                set_status('扫描中 %d/%d…' % (done, len(nodes)))
            for v in vids:
                total += 1
                if not v['isPassed']:
                    pending += 1
                    pend_secs += v['duration']
                rows.append({'node': n['id'],
                             'title': (n['title'] or '')[:40],
                             'dur': v['duration'],
                             'state': '已完成' if v['isPassed'] else '未完成'})
            if done % 10 == 0:
                with LOCK:
                    STATE['tree'] = list(rows)
    with LOCK:
        STATE['tree'] = rows
    summary = '视频任务点 %d 个，未完成 %d 个，合计约 %d 秒（约 %.1f 小时）' % (
        total, pending, pend_secs, pend_secs / 3600.0)
    push_log(summary)
    with LOCK:
        STATE['summary'] = summary
        STATE['progress'] = {'done': 0, 'total': pending}
        LAST_SCAN['ts'] = time.time()
    return ctx


def do_probe(cookie, url, force=True):
    try:
        age = time.time() - LAST_SCAN['ts']
        if (not force) and STATE['tree'] and age < SCAN_COOLDOWN:
            push_log('跳过重复刷新（%d 秒前刚刷新过）' % int(age))
            set_status('状态已刷新（跳过重复）')
            return
        set_status('刷新中…')
        scan(cookie, url)
        set_status('状态已刷新')
    except Exception as exc:
        push_log('[错误] %r' % (exc,))
        set_status('出错')
    finally:
        with LOCK:
            STATE['busy'] = False
def plan_nodes(selected):
    """要跑的节点 = 勾选的 + 最后一个勾选之后所有未完成的。"""
    with LOCK:
        rows = list(STATE['tree'])
    order = [r['node'] for r in rows]
    sel = [str(s) for s in (selected or [])]
    if not sel:
        return [r['node'] for r in rows if r['state'] != '已完成'], False
    nodes = [s for s in sel if s in order] or sel
    idx = max((order.index(s) for s in nodes if s in order), default=-1)
    if idx >= 0:
        nodes += [r['node'] for r in rows[idx + 1:] if r['state'] != '已完成']
    seen, out = set(), []
    for n in nodes:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out, True      # 需要 --force-nodes，让选中的“已完成”也重跑


def do_run(cookie, url, p):
    try:
        set_status('自动观看中…')
        core.reset_stop()
        if not STATE['tree']:
            scan(cookie, url)
        nodes, force = plan_nodes(p.get('selected'))
        if not nodes:
            push_log('没有需要处理的节点')
            set_status('无可处理项')
            return
        argv = ['--cookie', cookie, '--from-url', url,
                '--interval', str(p.get('interval') or 62),
                '--rate', str(p.get('rate') or 1.0),
                '--nodes', ','.join(nodes)]
        if force:
            argv.append('--force-nodes')
        if p.get('restart'):
            argv.append('--restart')
        try:
            if float(p.get('max_minutes') or 0) > 0:
                argv += ['--max-minutes', str(float(p['max_minutes']))]
        except Exception:
            pass
        if p.get('dry_run'):
            argv.append('--dry-run')
        push_log('本次计划处理 %d 个节点：%s%s'
                 % (len(nodes), ','.join(nodes[:6]),
                    ' …' if len(nodes) > 6 else ''))
        with contextlib.redirect_stdout(_Writer(push_log)), \
                contextlib.redirect_stderr(_Writer(push_log)):
            try:
                core.main(argv)
            except SystemExit as exc:
                push_log('[退出] %s' % (exc,))
        try:                     # 跑完自动刷新一次状态
            scan(cookie, url)
        except Exception:
            pass
        set_status('结束')
    except Exception as exc:
        push_log('[错误] %r' % (exc,))
        set_status('出错')
    finally:
        with LOCK:
            STATE['busy'] = False


def start_thread(fn, *a):
    with LOCK:
        if STATE['busy']:
            return False
        STATE['busy'] = True
    t = threading.Thread(target=fn, args=a, daemon=True)
    WORKER['thread'] = t
    t.start()
    return True


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>学习通 · 课程视频自动完成</title>
<style>
 body{font:14px/1.6 "Microsoft YaHei",system-ui,sans-serif;margin:0;background:#f5f6f8;color:#222}
 .wrap{max-width:1080px;margin:0 auto;padding:18px}
 h1{font-size:18px;margin:0 0 12px}
 .card{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:14px;margin-bottom:14px}
 label{display:block;font-size:12px;color:#666;margin:8px 0 4px}
 input[type=text],textarea{width:100%;box-sizing:border-box;border:1px solid #cdd3da;border-radius:6px;padding:7px 9px;font:inherit}
 textarea{height:64px;resize:vertical}
 .row{display:flex;gap:12px}.row>div{flex:1}
 button{border:0;border-radius:7px;padding:9px 16px;font:inherit;cursor:pointer;background:#2b6cb0;color:#fff}
 button.gray{background:#718096}button.green{background:#2f855a}button:disabled{opacity:.5;cursor:not-allowed}
 .bar{display:flex;gap:10px;align-items:center;margin-bottom:14px;flex-wrap:wrap}
 .pill{margin-left:auto;background:#edf2f7;border-radius:20px;padding:4px 14px;font-size:13px;color:#444}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{border-bottom:1px solid #eef1f4;padding:6px 8px;text-align:left}
 th{background:#fafbfc;color:#555;font-weight:600}
 tr.done{color:#8a8f96}
 .ok{color:#1a7f37;font-weight:600}.no{color:#b7791f}
 .prog{height:8px;background:#e6ebf1;border-radius:5px;overflow:hidden;margin:6px 0 2px}
 .prog>i{display:block;height:100%;width:0;background:#3182ce;transition:width .3s}
 pre#log{margin:0;height:280px;overflow:auto;white-space:pre-wrap;font:12px/1.5 Consolas,monospace;background:#0f172a;color:#d7e3f4;border-radius:8px;padding:10px}
</style></head><body><div class="wrap">
<h1>学习通 · 课程视频自动完成</h1>

<div class="card">
  <label>Cookie（Reqable 里任一条 chaoxing 请求的 Cookie 头，整段粘贴）</label>
  <textarea id="cookie" placeholder="fid=...; _uid=...; UID=...; vc3=...; uf=...;"></textarea>
  <label>studentstudy 链接（整条粘贴即可，多余参数会被忽略）</label>
  <input type="text" id="url" placeholder="https://mooc1.chaoxing.com/mycourse/studentstudy?chapterId=...&courseId=...&clazzid=...&cpi=...">
  <div class="row">
    <div><label>上报间隔(秒)</label><input type="text" id="interval" value="60"></div>
    <div><label>速率 1.0=正常 2.0=双倍</label><input type="text" id="rate" value="1.0"></div>
    <div><label>限时(分钟, 0=不限)</label><input type="text" id="maxmin" value="0"></div>
    <div><label>重跑时起始方式</label><select id="startmode" style="width:100%;box-sizing:border-box;border:1px solid #cdd3da;border-radius:6px;padding:7px 9px;font:inherit">
      <option value="resume">续看（服务端记录的位置）</option>
      <option value="restart">从头（0 开始重刷）</option>
    </select></div>
  </div>
  <label style="display:flex;align-items:center;gap:6px;margin-top:10px">
    <input type="checkbox" id="autorefresh" checked style="width:auto"> 打开页面时自动刷新状态列表
  </label>
</div>

<div class="bar">
  <button class="gray" id="bProbe" onclick="probe()">刷新状态</button>
  <button id="bRun" onclick="runAll()">开始全部未完成</button>
  <button class="green" id="bSel" onclick="runSelected()">从选中开始继续</button>
  <button class="gray" id="bStop" onclick="stop()" disabled>停止</button>
  <span class="pill" id="status">就绪</span>
</div>

<div class="card">
  <div id="summary" style="margin-bottom:6px;color:#555"></div>
  <div class="prog"><i id="pbar"></i></div>
  <div id="ptext" style="font-size:12px;color:#777;margin-bottom:8px"></div>
  <table><thead><tr>
    <th style="width:34px"><input type="checkbox" id="all" onclick="toggleAll(this)"></th>
    <th style="width:120px">节点ID</th><th>标题</th>
    <th style="width:90px">时长(秒)</th><th style="width:100px">状态</th>
  </tr></thead><tbody id="tb"></tbody></table>
  <div style="font-size:12px;color:#777;margin-top:8px">
    勾选后点「从选中开始继续」：会先重跑勾选的视频（即使显示已完成），再继续把它后面的未完成视频做完；不勾选就是全部未完成。
  </div>
</div>

<div class="card"><label style="margin-top:0">运行日志</label><pre id="log"></pre></div>
</div>
<script>
let cursor=0, loaded=false, lastSig='';
const PRESET = __PRESET__;
function val(id){return document.getElementById(id).value.trim()}
function body(force){return JSON.stringify({cookie:val('cookie'),url:val('url'),
  params:{interval:val('interval'),rate:val('rate'),max_minutes:val('maxmin'),
          restart:val('startmode')==='restart',force:!!force,
          selected:selected(),dry_run:false}})}
function selected(){return Array.from(document.querySelectorAll('#tb input[type=checkbox]:checked')).map(c=>c.dataset.node)}
function toggleAll(box){document.querySelectorAll('#tb input[type=checkbox]').forEach(c=>c.checked=box.checked)}
async function post(path,force){
  try{
    const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:body(force)});
    const j=await r.json(); if(j.error) alert(j.error);
  }catch(e){alert('请求失败: '+e)}
}
async function probe(){await post('/api/probe',true)}      // 手动：强制刷新
async function autoProbe(){await post('/api/probe',false)}  // 自动：带去重守卫
async function runAll(){await post('/api/run')}
async function runSelected(){
  if(selected().length===0 && !confirm('没有勾选任何视频。\n是否按“全部未完成”开始？')) return;
  await post('/api/run');
}
async function stop(){await post('/api/stop')}
async function poll(){
  try{
    const r=await fetch('/api/state?since='+cursor); const j=await r.json();
    if(!loaded){ loaded=true;               // 首次拿到状态时只触发一次自动刷新
      if(document.getElementById('autorefresh').checked && val('cookie') && val('url') && !j.busy) autoProbe();
    }
    document.getElementById('status').textContent=j.status;
    document.getElementById('summary').textContent=j.summary||'';
    const busy=j.busy;
    for(const id of ['bProbe','bRun','bSel']) document.getElementById(id).disabled=busy;
    document.getElementById('bStop').disabled=!busy;
    const sig=j.tree.map(r=>r.node+r.state).join(',');
    if(sig!==lastSig){ lastSig=sig;          // 只在数据变化时重建表格（避免每秒闪烁）
    const keep=selected();
    const tb=document.getElementById('tb'); tb.innerHTML='';
    for(const row of j.tree){
      const tr=document.createElement('tr');
      if(row.state==='已完成') tr.className='done';
      const td0=document.createElement('td');
      const cb=document.createElement('input'); cb.type='checkbox'; cb.dataset.node=row.node;
      cb.checked=keep.indexOf(row.node)>=0; td0.appendChild(cb); tr.appendChild(td0);
      for(const t of [row.node,row.title,row.dur,row.state]){
        const td=document.createElement('td'); td.textContent=t; tr.appendChild(td);
      }
      tr.lastChild.className=(row.state==='已完成'?'ok':'no');
      tb.appendChild(tr);
    }
    }
    const p=j.progress||{done:0,total:0};
    document.getElementById('ptext').textContent = p.total? ('进度 '+p.done+' / '+p.total):'';
    document.getElementById('pbar').style.width = p.total? Math.round(100*p.done/p.total)+'%':'0%';
    if(j.logs.length){
      const pre=document.getElementById('log');
      const stick=pre.scrollTop+pre.clientHeight>=pre.scrollHeight-20;
      pre.textContent+=j.logs.join('\n')+'\n'; cursor=j.cursor;
      if(stick) pre.scrollTop=pre.scrollHeight;
    }
  }catch(e){}
}
window.addEventListener('load',()=>{
  const c=document.getElementById('cookie'), u=document.getElementById('url');
  if(!c.value && PRESET.cookie) c.value=PRESET.cookie;
  if(!u.value && PRESET.url) u.value=PRESET.url;
});
setInterval(poll,1000); poll();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = 'chaoxing-webui/1.1'

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith('/api/state'):
            since = 0
            m = re.search(r'since=(\d+)', self.path)
            if m:
                since = int(m.group(1))
            with LOCK:
                logs = STATE['logs'][since:]
                self._json({'status': STATE['status'], 'busy': STATE['busy'],
                            'tree': STATE['tree'], 'logs': logs,
                            'summary': STATE['summary'],
                            'progress': STATE['progress'],
                            'cursor': len(STATE['logs'])})
            return
        page = PAGE.replace('__PRESET__',
                            json.dumps(PRESET, ensure_ascii=False)).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(page)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(page)

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length).decode('utf-8', 'replace')
        try:
            data = json.loads(raw or '{}')
        except Exception:
            data = {}
        cookie = (data.get('cookie') or '').strip()
        url = (data.get('url') or '').strip()
        params = data.get('params') or {}

        if self.path.startswith('/api/stop'):
            core.request_stop()
            push_log('[已发送停止指令]')
            self._json({'ok': True})
            return
        if not cookie or not url:
            self._json({'error': '请先填写 cookie 和 studentstudy 链接'})
            return
        if self.path.startswith('/api/probe'):
            force = bool(params.get('force', True))
            self._json({'ok': start_thread(do_probe, cookie, url, force)})
            return
        if self.path.startswith('/api/run'):
            self._json({'ok': start_thread(do_run, cookie, url, params)})
            return
        self._json({'error': 'unknown api'}, 404)


def free_port(preferred):
    for port in [preferred, 8757, 8758, 9002, 9003]:
        with socket.socket() as s:
            try:
                s.bind(('127.0.0.1', port))
                return port
            except OSError:
                continue
    raise SystemExit('找不到可用端口')


def main(argv=None):
    ap = argparse.ArgumentParser(description='学习通自动观看 · 本机网页版')
    ap.add_argument('--port', type=int, default=8756)
    ap.add_argument('--open-browser', action='store_true',
                    help='启动后自动打开本机网页界面')
    ap.add_argument('--defaults', default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'webui_defaults.json'))
    args = ap.parse_args(argv)
    global PRESET
    try:
        with open(args.defaults, 'r', encoding='utf-8') as fh:
            PRESET = json.load(fh) or {}
    except Exception:
        PRESET = {}
    port = free_port(args.port)
    url = 'http://127.0.0.1:%d/' % port
    srv = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print('学习通自动观看 · 网页版已启动: %s' % url)
    if args.open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n已退出')


if __name__ == '__main__':
    main()
