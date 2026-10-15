#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chaoxing_autowatch.py — 学习通(超星)课程视频自动完成工具（可复用 CLI）。

设计要点（全部经过实测验证，见 forge_findings.md 的 E14–E28）：
  * 日志签名 enc 本地计算：
      md5("[clazzId][userid][jobid][objectId][playingTime*1000][d_yHJ!$pdA~5][duration*1000][clipTime]")
  * dtoken 来源：GET /ananas/status/<objectId>?fid=<fid>（必须带视频模块 Referer，否则 403）
  * 每个视频节点的 objectId/jobid/attDuration/attDurationEnc/otherInfo 来自 knowledge/cards
  * 服务端按「真实时间间隔」累计有效观看时长，`isPassed` 才是权威完成标志
    → 因此本工具按固定墙钟节奏（默认 62s）逐条上报，无法压缩这段真实时间。

用法示例：
  # 1) 只看课程结构/哪些视频没完成（只读，安全）
  python chaoxing_autowatch.py --cookie-file cookie.txt \
      --from-url "https://mooc1.chaoxing.com/mycourse/studentstudy?chapterId=...&courseId=...&clazzid=...&cpi=..." \
      --probe

  # 2) 自动完成所有未完成的视频（按真实节奏）
  python chaoxing_autowatch.py --cookie-file cookie.txt --from-url "..." --rate 1.0

  # 3) 只做指定节点，限时 30 分钟
  python chaoxing_autowatch.py --cookie-file cookie.txt --from-url "..." \
      --nodes 1262759012,1262759013 --max-minutes 30

cookie 从 Reqable 任一条 chaoxing 请求的请求头里整段复制即可（含 fid/_uid/vc3 等）。
"""

import argparse
import hashlib
import json
import os
import random
import re
import socket
import ssl
import sys
import time
from urllib.parse import parse_qsl, urlparse

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# 供 GUI 使用：外部可调用 request_stop() 让循环尽快停下
STOP_FLAG = {'stop': False}


def request_stop():
    STOP_FLAG['stop'] = True


def reset_stop():
    STOP_FLAG['stop'] = False


def stop_requested():
    return bool(STOP_FLAG['stop'])

HOST = 'mooc1.chaoxing.com'
KEY = 'd_yHJ!$pdA~5'
VIDEO_MODULE_REFERER = ('https://mooc1.chaoxing.com/ananas/modules/video/'
                        'index.html?v=2026-0920-1154')
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36 Edg/155.0.0.0')

ISDRAG = {'playing': 0, 'drag': 1, 'pause': 2, 'play': 3, 'ended': 4}


# --------------------------------------------------------------------------- #
# HTTP (raw socket, 与已验证的实现一致)
# --------------------------------------------------------------------------- #
class HttpClient:
    def __init__(self, cookie, timeout=25, verbose=False):
        self.cookie = cookie
        self.timeout = timeout
        self.verbose = verbose

    def get(self, url, referer=None, xhr=False):
        u = urlparse(url)
        if u.scheme not in ('http', 'https') or not u.hostname:
            raise ValueError('bad url: %r' % url)
        port = u.port or (443 if u.scheme == 'https' else 80)
        path = u.path + (('?' + u.query) if u.query else '')
        headers = [
            'GET %s HTTP/1.1' % path,
            'Host: %s' % u.hostname,
            'User-Agent: ' + UA,
            'Accept: */*',
            'Accept-Encoding: identity',
            'Cookie: ' + self.cookie,
            'Connection: close',
        ]
        if xhr:
            headers.append('X-Requested-With: XMLHttpRequest')
        if referer:
            headers.append('Referer: ' + referer)
        if 'multimedia/log' in path:
            headers.append('Content-Type: application/json')

        raw = socket.create_connection((u.hostname, port), timeout=self.timeout)
        try:
            if u.scheme == 'https':
                ctx = ssl.create_default_context()
                raw = ctx.wrap_socket(raw, server_hostname=u.hostname)
            raw.sendall(('\r\n'.join(headers) + '\r\n\r\n').encode())
            data = b''
            while True:
                chunk = raw.recv(16384)
                if not chunk:
                    break
                data += chunk
        finally:
            try:
                raw.close()
            except Exception:
                pass
        head, _, body = data.partition(b'\r\n\r\n')
        status_line = head.split(b'\r\n')[0].decode('latin-1') if head else ''
        text = body.decode('utf-8', 'replace')
        code = 0
        m = re.match(r'HTTP/\d(?:\.\d)?\s+(\d{3})', status_line)
        if m:
            code = int(m.group(1))
        if self.verbose:
            print('    [http] %s -> %s (%d bytes)' % (url.split('?')[0], code, len(body)))
        return code, text


# --------------------------------------------------------------------------- #
# 数据模型 / 解析
# --------------------------------------------------------------------------- #
def parse_studentstudy_url(url):
    """从 studentstudy 链接里取 courseId/clazzid/cpi/chapterId/enc。"""
    q = dict(parse_qsl(urlparse(url).query, keep_blank_values=True))
    out = {
        'courseId': q.get('courseId'),
        'clazzid': q.get('clazzid') or q.get('clazzId'),
        'cpi': q.get('cpi'),
        'chapterId': q.get('chapterId'),
        'enc': q.get('enc'),
    }
    missing = [k for k in ('courseId', 'clazzid', 'cpi') if not out[k]]
    if missing:
        raise SystemExit('URL 缺少参数: %s' % ','.join(missing))
    return out


def extract_json_array(text, key):
    """从 HTML/JS 中稳健地取出 "key":[...] 的 JSON 数组并解析。"""
    idx = text.find('"%s":[' % key)
    if idx < 0:
        idx = text.find('%s":[' % key)
    if idx < 0:
        return None
    start = text.find('[', idx)
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == '[':
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0:
                # 卡片携带的字符串常含未转义控制字符，必须 strict=False
                return json.loads(text[start:i + 1], strict=False)
    return None


def list_nodes(client, ctx):
    """列出课程所有节点 id（含章节分组，分组没有视频时会被自动跳过）。"""
    url = ('https://%s/mooc-ans/mycourse/studentstudycourselist'
           '?courseId=%s&chapterId=%s&clazzid=%s&cpi=%s&mooc2=1'
           '&searchChapterListByName=' % (HOST, ctx['courseId'],
                                          ctx.get('chapterId') or '',
                                          ctx['clazzid'], ctx['cpi']))
    code, body = client.get(url, xhr=True)
    if code != 200:
        raise SystemExit('节点列表请求失败: HTTP %d' % code)
    nodes = []
    for m in re.finditer(r'id="cur(\d+)"', body):
        node_id = m.group(1)
        seg = body[m.start():m.start() + 600]
        title = re.search(r'title="([^"]*)"', seg)
        cnt = re.search(r'value="(\d+)"\s+class="jobUnfinishCount"', seg)
        nodes.append({
            'id': node_id,
            'title': (title.group(1) if title else ''),
            'unfinished': int(cnt.group(1)) if cnt else 0,
        })
    # 去重保序
    seen, uniq = set(), []
    for n in nodes:
        if n['id'] not in seen:
            seen.add(n['id'])
            uniq.append(n)
    return uniq


def video_attachments(client, ctx, node_id):
    """取某节点的视频附件（objectId/jobid/时长/otherInfo/是否已通过）。"""
    url = ('https://%s/mooc-ans/knowledge/cards?clazzid=%s&courseid=%s'
           '&knowledgeid=%s&num=0&ut=s&cpi=%s&v=2025-0424-1038-3&mooc2=1'
           '&isMicroCourse=false&editorPreview=0'
           % (HOST, ctx['clazzid'], ctx['courseId'], node_id, ctx['cpi']))
    code, body = client.get(url, xhr=False)
    if code != 200:
        return []
    # 卡片里的 mArg JSON 偶尔含非法控制字符/截断，不用整体 json.loads，
    # 改为围绕每个 "objectId" 取局部窗口正则取值，更稳。
    out = []
    seen = set()
    for m in re.finditer(r'"objectId"\s*:\s*"([0-9a-fA-F]{16,64})"', body):
        oid = m.group(1)
        if oid in seen:
            continue
        seg = body[max(0, m.start() - 1600):m.start() + 700]
        if '"type":"video"' not in seg:
            continue

        def grab(pat, default=None, cast=None, src=seg):
            mm = re.search(pat, src)
            if not mm:
                return default
            val = mm.group(1)
            return cast(val) if cast else val

        job = grab(r'"jobid"\s*:\s*"(\d+)"')
        dur = grab(r'"attDuration"\s*:\s*(\d+)', 0, int)
        aenc = grab(r'"attDurationEnc"\s*:\s*"([0-9a-fA-F]+)"')
        other = grab(r'"otherInfo"\s*:\s*"([^"]+)"')
        passed = grab(r'"isPassed"\s*:\s*(true|false)', 'false') == 'true'
        off = grab(r'"headOffset"\s*:\s*(\d+)', 0, int)
        if not (job and dur and aenc and other):
            continue
        seen.add(oid)
        out.append({
            'node': node_id,
            'objectId': oid,
            'jobid': job,
            'duration': dur,
            'attDurationEnc': aenc,
            'otherInfo': other,
            'isPassed': passed,
            'headOffset': off,
        })
    return out


def fetch_dtoken(client, ctx, object_id):
    """GET /ananas/status/<objectId>?fid=<fid> —— dtoken 的唯一来源。"""
    url = 'https://%s/ananas/status/%s?fid=%s' % (HOST, object_id, ctx['fid'])
    code, body = client.get(url, referer=VIDEO_MODULE_REFERER, xhr=True)
    if code != 200:
        raise RuntimeError('status 接口 HTTP %d（检查 cookie/Referer）' % code)
    return json.loads(body)['dtoken']


# --------------------------------------------------------------------------- #
# 上报
# --------------------------------------------------------------------------- #
def build_log_url(ctx, video, dtoken, playing_time, isdrag, clip_time, ts_ms):
    ms = playing_time * 1000
    src = '[{0}][{1}][{2}][{3}][{4}][{5}][{6}][{7}]'.format(
        ctx['clazzid'], ctx['userid'], video['jobid'], video['objectId'],
        ms, KEY, video['duration'] * 1000, clip_time)
    enc = hashlib.md5(src.encode('utf-8')).hexdigest()
    return ('https://%s/mooc-ans/multimedia/log/a/%s/%s'
            '?clazzId=%s&playingTime=%d&duration=%d&clipTime=%s&objectId=%s'
            '&otherInfo=%s&courseId=%s&jobid=%s&userid=%s&isdrag=%d&view=pc'
            '&enc=%s&rt=0.9&videoFaceCaptureEnc=&dtype=Video&_t=%d'
            '&attDuration=%d&attDurationEnc=%s&courseEngineInfo=false'
            % (HOST, ctx['cpi'], dtoken, ctx['clazzid'], playing_time,
               video['duration'], clip_time, video['objectId'],
               video['otherInfo'], ctx['courseId'], video['jobid'],
               ctx['userid'], isdrag, enc, ts_ms, video['duration'],
               video['attDurationEnc']))


def report(client, ctx, video, dtoken, playing_time, isdrag, clip_time):
    url = build_log_url(ctx, video, dtoken, playing_time, isdrag, clip_time,
                        int(time.time() * 1000))
    code, body = client.get(url, referer=VIDEO_MODULE_REFERER, xhr=False)
    if code != 200:
        return {'_http': code, '_body': body.strip()[:200]}
    try:
        return json.loads(body)
    except Exception:
        return {'_http': code, '_body': body.strip()[:200]}


def _wait(seconds, slice_s=0.5):
    """可被 request_stop() 提前打断的等待。"""
    end = time.time() + seconds
    while time.time() < end:
        if stop_requested():
            return False
        time.sleep(min(slice_s, max(0.0, end - time.time())))
    return True


def watch_video(client, ctx, video, args):
    """按真实节奏上报直到 isPassed；返回 True/False。"""
    duration = video['duration']
    if duration <= 0:
        print('    ! 时长为 0，跳过')
        return False
    clip = '0_%d' % duration
    dtoken = fetch_dtoken(client, ctx, video['objectId'])
    restart = bool(getattr(args, 'restart', False))
    if restart:
        start_pt = 0                                   # 从头重刷
    else:
        start_pt = max(0, min(duration, video['headOffset'] // 1000))  # 续看
    step = max(1, int(round(args.interval * args.rate)))
    print('    dtoken=%s  时长=%ds  起始方式=%s  起点=%ds  每 %.0fs 报 +%ds'
          % (dtoken, duration, '从头' if restart else '续看', start_pt,
             args.interval, step))

    if args.dry_run:
        print('    [dry-run] 只展示第一条将要发送的请求')
        print('    ' + build_log_url(ctx, video, dtoken, min(duration, start_pt + step),
                                   0, clip, int(time.time() * 1000)))
        return False

    # 起始上报（isdrag=3 play）
    r = report(client, ctx, video, dtoken, start_pt, ISDRAG['play'], clip)
    print('    start pt=%-5d %s' % (start_pt, json.dumps(r, ensure_ascii=False)))

    pt = start_pt
    rounds = 0
    while pt < duration:
        rounds += 1
        if args.max_minutes and (time.time() - args.t_start) > args.max_minutes * 60:
            print('    ! 达到 --max-minutes 上限，暂停该视频（下次可续跑）')
            return False
        if stop_requested():
            print('    ! 收到停止指令，已中断（下次可重跑续做）')
            return False
        sleep_s = max(1.0, args.interval + random.uniform(-args.jitter, args.jitter))
        if not _wait(sleep_s):
            print('    ! 收到停止指令，已中断（下次可重跑续做）')
            return False
        pt = min(duration, pt + step)
        isdrag = ISDRAG['ended'] if pt >= duration else ISDRAG['playing']
        r = report(client, ctx, video, dtoken, pt, isdrag, clip)
        passed = bool(r.get('isPassed'))
        extra = ''
        if r.get('videoTimeLimit'):
            extra = '  [videoTimeLimit=true]'
        if r.get('hasJobLimit'):
            extra += '  [hasJobLimit=true]'
        print('    round %-2d t=%6.0fs pt=%-5d isdrag=%d isPassed=%s%s'
              % (rounds, time.time() - args.t_start, pt, isdrag, passed, extra))
        sys.stdout.flush()
        if passed:
            return True
        if pt >= duration:
            # 到片尾仍未通过：再多等一轮，很多情况下服务端在末次上报后才结算
            break
    # 末次确认
    for _ in range(args.final_retries):
        time.sleep(args.interval)
        r = report(client, ctx, video, dtoken, duration, ISDRAG['ended'], clip)
        if r.get('isPassed'):
            print('    -> 末次确认通过')
            return True
    return False


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv=None):
    p = argparse.ArgumentParser(description='学习通课程视频自动完成（可复用）')
    p.add_argument('--cookie', default=os.environ.get('CX_COOKIE'),
                   help='整段 cookie；也可用 CX_COOKIE 环境变量')
    p.add_argument('--cookie-file', help='cookie 文件（一行）')
    p.add_argument('--from-url', dest='from_url',
                   help='任意 studentstudy 链接，自动解析 courseId/clazzid/cpi/chapterId')
    p.add_argument('--course-id')
    p.add_argument('--clazz-id')
    p.add_argument('--cpi')
    p.add_argument('--userid', help='缺省从 cookie 的 _uid 推断')
    p.add_argument('--fid', help='缺省从 cookie 的 fid 推断')
    p.add_argument('--nodes', help='只处理这些节点，逗号分隔')
    p.add_argument('--interval', type=float, default=60.0, help='上报间隔秒，默认 60')
    p.add_argument('--rate', type=float, default=1.0,
                   help='每秒墙钟推进多少秒视频：1.0=正常，2.0=2 倍速，0.5=半速。默认 1.0')
    p.add_argument('--jitter', type=float, default=2.0, help='间隔抖动秒，默认 2')
    p.add_argument('--final-retries', type=int, default=1,
                   help='到片尾后的额外确认次数，默认 1')
    p.add_argument('--max-minutes', type=float, default=0, help='总运行时长上限（分钟）')
    p.add_argument('--max-nodes', type=int, default=0, help='最多处理多少个视频')
    p.add_argument('--force-nodes', action='store_true', dest='force_nodes',
                   help='--nodes 指定的节点即使已完成也强制重跑')
    p.add_argument('--restart', action='store_true',
                   help='重跑时从头(0)开始上报；不加则按服务端记录的续看位置')
    p.add_argument('--probe', action='store_true', help='只列出结构，不发任何上报')
    p.add_argument('--dry-run', action='store_true', help='不发上报，只打印将发送的 URL')
    p.add_argument('--verbose', action='store_true')
    args = p.parse_args(argv)
    args.t_start = time.time()
    reset_stop()

    cookie = args.cookie
    if args.cookie_file:
        with open(args.cookie_file, 'r', encoding='utf-8') as fh:
            cookie = fh.read().strip()
    if not cookie:
        raise SystemExit('缺少 cookie：用 --cookie / --cookie-file / CX_COOKIE')

    def cookie_value(name, default=None):
        m = re.search(r'(?:^|;\s*)%s=([^;]*)' % re.escape(name), cookie)
        return m.group(1) if m else default

    if args.from_url:
        ctx = parse_studentstudy_url(args.from_url)
        chapter = ctx.get('chapterId')
    else:
        if not (args.course_id and args.clazz_id and args.cpi):
            raise SystemExit('需要 --from-url，或同时提供 --course-id/--clazz-id/--cpi')
        ctx = {'courseId': args.course_id, 'clazzid': args.clazz_id,
               'cpi': args.cpi, 'chapterId': None}
        chapter = None
    ctx['userid'] = args.userid or cookie_value('_uid') or cookie_value('UID')
    ctx['fid'] = args.fid or cookie_value('fid')
    if not ctx['userid'] or not ctx['fid']:
        raise SystemExit('无法确定 userid/fid，请显式传 --userid/--fid')
    if chapter:
        ref = ('https://%s/mycourse/studentstudy?chapterId=%s&courseId=%s'
               '&clazzid=%s&cpi=%s' % (HOST, chapter, ctx['courseId'],
                                       ctx['clazzid'], ctx['cpi']))
    else:
        ref = 'https://%s/mycourse/studentstudy' % HOST

    client = HttpClient(cookie, verbose=args.verbose)
    print('课程 %s / 班级 %s / cpi %s / 用户 %s'
          % (ctx['courseId'], ctx['clazzid'], ctx['cpi'], ctx['userid']))

    print('\n[1/3] 读取节点列表 ...')
    nodes = list_nodes(client, ctx)
    print(' 共 %d 个节点' % len(nodes))

    wanted = set(args.nodes.split(',')) if args.nodes else None
    print('\n[2/3] 扫描视频任务点 ...')
    targets = []
    for n in nodes:
        if wanted and n['id'] not in wanted:
            continue
        try:
            vids = video_attachments(client, ctx, n['id'])
        except Exception as exc:
            print('  节点 %s 读取失败: %r' % (n['id'], exc))
            continue
        for v in vids:
            state = '已完成' if v['isPassed'] else '未完成'
            print('  节点 %-11s %-14s 时长=%-4ds %s'
                  % (n['id'], (n['title'] or '')[:14], v['duration'], state))
            forced = bool(getattr(args, 'force_nodes', False)) and wanted and n['id'] in wanted
            if (not v['isPassed']) or forced:
                targets.append((n, v))

    print('\n 待完成视频 %d 个，合计约 %d 秒视频时长'
          % (len(targets), sum(v['duration'] for _, v in targets)))
    if args.probe:
        print('\n--probe：结束（未发送任何上报）')
        return 0
    if not targets:
        print('没有需要处理的视频。')
        return 0

    print('\n[3/3] 开始自动观看 ...')
    done = failed = 0
    for idx, (n, v) in enumerate(targets, 1):
        if args.max_nodes and done >= args.max_nodes:
            print('达到 --max-nodes 限制，停止')
            break
        if stop_requested():
            print('收到停止指令，停止')
            break
        if args.max_minutes and (time.time() - args.t_start) > args.max_minutes * 60:
            print('达到 --max-minutes 限制，停止')
            break
        print('\n[%d/%d] 节点 %s (%s) 时长 %ds'
              % (idx, len(targets), n['id'], n['title'], v['duration']))
        try:
            ok = watch_video(client, ctx, v, args)
        except Exception as exc:
            print('    出错: %r' % (exc,))
            ok = False
        if ok:
            done += 1
            print('    ✅ 已通过')
        else:
            failed += 1
            print('    ⚠️ 未确认通过（可重跑续做）')

    print('\n完成 %d 个，未确认 %d 个，总耗时 %.1f 分钟'
          % (done, failed, (time.time() - args.t_start) / 60.0))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
