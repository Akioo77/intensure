#!/usr/bin/env python3
"""用 GitHub REST API 推送 commit —— 解决「github.com:443 不通、只能走 api.github.com」的环境问题。

## 为什么需要这个工具

某些网络环境下 **github.com 的 443 端口连不通**（但 api.github.com 通、SSH 22 也通），
于是 `git push` 会一直卡在 TCP connect。`gh` CLI 已用 keyring 登录、token 带 `repo` scope，
所以改走 GitHub **REST API**（Git Data API）推送：
`POST /git/blobs` → `POST /git/trees` → `POST /git/commits` → `PATCH /git/refs/heads/<branch>`。

## 为什么还要「UTC 对齐」

GitHub API 会把 author/committer 的时间**归一到 UTC**（`2026-11-02T19:43:45Z`），
而本地 git 提交默认记的是 `+08:00` 偏移量。git 的 commit sha 由
`tree + parent + author + committer + message` 算出，时间字符串不同 → **sha 就不同**。
结果就是：本地和远端看着是同一个改动，sha 却不同，下次 `git pull` / `git push` 会报分叉。

本工具的解法：**把本地提交也重建成 UTC**，让本地 sha 与远端 sha **逐字节相同**。

## 用法

    python3 tools/api_push.py                # 推当前 HEAD 到 master
    python3 tools/api_push.py --branch dev   # 推当前 HEAD 到 dev

要求：本地提交父节点已在远端（不支持一次推多个 commit 的场景）。
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

REPO = os.environ.get('GITHUB_REPO', 'Akioo77/intensure')
API = f'https://api.github.com/repos/{REPO}'
GH = os.environ.get('GH_BIN', 'gh')


def gh_token():
    r = subprocess.run([GH, 'auth', 'token'], capture_output=True, text=True)
    tok = r.stdout.strip()
    if not tok:
        raise SystemExit('❌ 拿不到 GitHub token（先 `gh auth login`）')
    return tok


def req(token, method, path, data=None, retries=4):
    """发一个 API 请求，失败自动重试。

    ⚠️ 为什么必须重试（2026-11-03 实测）：
    本机到 api.github.com 的链路**本身是抖的** —— 连续小请求的 HTTP 码在
    200 / 000 / 200 之间跳（约一半成功率）。一次抖动就会把整个推送打断
    （实测挂在第 3 个 blob 上）。

    重试是安全的：blob / tree / commit 都是**内容寻址**的，
    同样的输入重发只会得到同样的 sha，不会产生垃圾对象。
    """
    body = json.dumps(data).encode() if data is not None else None
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = urllib.request.Request(
                f'{API}{path}', data=body, method=method,
                headers={
                    'Authorization': f'Bearer {token}',
                    'Accept': 'application/vnd.github+json',
                    'Content-Type': 'application/json',
                    'User-Agent': 'intensure-api-push',
                })
            with urllib.request.urlopen(r, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            # 4xx 是真错误（例如 ref 非快进），重试没有意义
            if e.code < 500:
                raise
            last = e
        except Exception as e:
            last = e
        if attempt < retries:
            delay = 2 * attempt
            print(f'   ↻ {method} {path} 第 {attempt} 次失败（{type(last).__name__}），'
                  f'{delay}s 后重试')
            time.sleep(delay)
    raise last


def git(*args, **kw):
    r = subprocess.run(['git'] + list(args), capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise SystemExit(f'❌ git {" ".join(args)} 失败:\n{r.stderr.strip()}')
    return r.stdout.strip()


def _date(iso_local: str) -> str:
    """**原样返回**，不做任何时区转换。

    GitHub 存 commit 对象时，author/committer 的时区偏移**按原样保留**：
    本地对象是 `... 1793784487 +0800` 就必须发 +08:00 的 ISO 串；
    转成 UTC 会让远端存成 +0000 -> 字节不同 -> sha 必然对不上（踩过）。

    顺带：`git commit-tree` 重建的 commit 对象里是 +0000，%aI 也会显示 +00:00，
    原样发就对得上 —— 所以**一律原样发**，不用分类讨论。
    """
    return iso_local


def commit_message(sha: str) -> str:
    """取 commit 对象里消息的**原始字节**（不做任何 strip）。

    为什么不能用 %B + rstrip 猜尾换行：
    - git commit 建的 commit，对象里消息**带**尾换行
    - git commit-tree -F file（文件末尾无换行）建的，对象里**不带**
    GitHub API 存的是原样字节 —— 猜错就 sha 必然对不上
    （踩过：本地 232abdfa 消息 542 字节，rstrip 后发 541，远端 sha 一直对不上）。
    """
    raw = subprocess.run(['git', 'cat-file', 'commit', sha],
                        capture_output=True).stdout
    if b'\n\n' not in raw:
        return ''
    return raw.split(b'\n\n', 1)[1].decode('utf-8')


def push_chain(token, branch, commits, remote_head):
    """链式推送多个 commit：按顺序建 commit，最后只更新一次 ref。

    为什么需要：一次只推一个 commit 时，工具会把本地 HEAD 重建成「UTC 版」sha，
    而下一个 commit 的父节点还是原来的本地 sha —— 父节点对不上就推不动了。
    链式推送让每个 commit 的远端父节点 = 上一个新建出来的 sha。
    """
    parent_remote = remote_head
    parent_tree = req(token, 'GET', f'/git/commits/{remote_head}')['tree']['sha']
    created = []
    for c in commits:
        files = [f for f in git('diff-tree', '--no-commit-id', '--name-only', '-r',
                                c).split('\n') if f.strip()]
        items = []
        for path in files:
            # 从 **commit 的树**读内容，不能读工作区：
            # 读工作区推的是「当前工作区」而非「这个 commit 的内容」，
            # 工作区一有未提交改动，远端 tree 就和本地 commit 对不上 -> sha 必然不同。
            content = subprocess.run(
                ['git', 'cat-file', 'blob', f'{c}:{path}'],
                capture_output=True).stdout
            if not content:
                items.append({'path': path, 'mode': '100644', 'type': 'blob', 'sha': None})
                continue
            b64 = base64.b64encode(content).decode()
            blob = req(token, 'POST', '/git/blobs', {'content': b64, 'encoding': 'base64'})
            mode = git('ls-files', '-s', '--', path).split()[0]
            items.append({'path': path, 'mode': mode, 'type': 'blob', 'sha': blob['sha']})
        tree = req(token, 'POST', '/git/trees',
                   {'base_tree': parent_tree, 'tree': items})['sha']
        message = commit_message(c)
        author = {'name': git('log', '-1', '--format=%an', c),
                  'email': git('log', '-1', '--format=%ae', c),
                  'date': _date(git('log', '-1', '--format=%aI', c))}
        committer = {'name': git('log', '-1', '--format=%cn', c),
                     'email': git('log', '-1', '--format=%ce', c),
                     'date': _date(git('log', '-1', '--format=%cI', c))}
        new_commit = req(token, 'POST', '/git/commits', {
            'message': message, 'tree': tree, 'parents': [parent_remote],
            'author': author, 'committer': committer,
        })['sha']
        print(f'  {c[:8]} -> {new_commit[:8]}  ({len(files)} files)')
        created.append((c, new_commit))
        parent_remote = new_commit
        parent_tree = tree
    req(token, 'PATCH', f'/git/refs/heads/{branch}',
        {'sha': parent_remote, 'force': False})
    print(f'OK remote {branch} -> {parent_remote} ({len(created)} commits)')
    return created


def rebuild_local_chain(commits, base):
    """把本地这串 commit 也重建成 UTC 版（与远端逐个 sha 对齐）。"""
    parent = base
    mapping = []
    for c in commits:
        tree = git('rev-parse', f'{c}^{{tree}}')
        message = commit_message(c)
        env = dict(os.environ)
        env.update({
            'GIT_AUTHOR_NAME': git('log', '-1', '--format=%an', c),
            'GIT_AUTHOR_EMAIL': git('log', '-1', '--format=%ae', c),
            'GIT_AUTHOR_DATE': _date(git('log', '-1', '--format=%aI', c)),
            'GIT_COMMITTER_NAME': git('log', '-1', '--format=%cn', c),
            'GIT_COMMITTER_EMAIL': git('log', '-1', '--format=%ce', c),
            'GIT_COMMITTER_DATE': _date(git('log', '-1', '--format=%cI', c)),
        })
        msg_file = os.path.join(os.environ.get('TMPDIR', '/tmp'), '_apipush_chain_msg')
        with open(msg_file, 'w', encoding='utf-8') as fh:
            fh.write(message)
        new = subprocess.run(
            ['git', 'commit-tree', tree, '-p', parent, '-F', msg_file],
            capture_output=True, text=True, env=env).stdout.strip()
        os.remove(msg_file)
        mapping.append((c, new))
        parent = new
    return mapping, parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--branch', default='master')
    ap.add_argument('--commit', default='HEAD')
    ap.add_argument('--all', action='store_true',
                    help='链式推送远端 head 到 HEAD 之间的所有 commit')
    ap.add_argument('--force', action='store_true',
                    help='远端 head 与本地 parent 不一致时仍强制更新（内容需等价）')
    args = ap.parse_args()

    commit = git('rev-parse', args.commit)
    parent = git('rev-parse', f'{commit}^')
    tree = git('rev-parse', f'{commit}^{{tree}}')
    # 消息**不带尾换行**：API 会按 git 惯例自己补一个 \n 再存。
    # 本地 commit-tree 也按同一套规范化 → 两边字节一致，sha 相同。
    # （反过来多给一个 \n，远端会变成 body+\n\n，sha 必然对不齐 —— 踩过）
    message = commit_message(commit)
    author = {
        'name': git('log', '-1', '--format=%an', commit),
        'email': git('log', '-1', '--format=%ae', commit),
        'date': _date(git('log', '-1', '--format=%aI', commit)),
    }
    committer = {
        'name': git('log', '-1', '--format=%cn', commit),
        'email': git('log', '-1', '--format=%ce', commit),
        'date': _date(git('log', '-1', '--format=%cI', commit)),
    }
    files = [f for f in git('diff-tree', '--no-commit-id', '--name-only', '-r',
                             commit).split('\n') if f.strip()]

    print(f'commit   : {commit}')
    print(f'parent   : {parent}')
    print(f'tree     : {tree}')
    print(f'author   : {author["name"]} {author["date"]}')
    print(f'files    : {files}')

    token = gh_token()

    if args.all:
        ref = req(token, 'GET', f'/git/ref/heads/{args.branch}')
        remote_head = ref['object']['sha']
        commits = [c for c in git('rev-list', '--reverse',
                                  f'{remote_head}..{commit}').split('\n') if c]
        if not commits:
            print('没有需要推送的 commit')
            return 0
        print(f'链式推送 {len(commits)} 个 commit（远端 head {remote_head[:8]}）')
        created = push_chain(token, args.branch, commits, remote_head)
        mapping, local_head = rebuild_local_chain([c for c, _ in created], remote_head)
        for (local_c, remote_c), (_, local_new) in zip(created, mapping):
            ok = 'OK ' if remote_c == local_new else 'WARN'
            print(f'  {ok} {local_c[:8]} local={local_new[:8]} remote={remote_c[:8]}')
        git('update-ref', f'refs/heads/{args.branch}', local_head, commit)
        git('update-ref', f'refs/remotes/origin/{args.branch}', local_head)
        print(f'OK 本地 master 已对齐 -> {local_head[:8]}')
        return 0

    # 远端父节点必须就是本地父节点，否则推上去会变成非快进
    ref = req(token, 'GET', f'/git/ref/heads/{args.branch}')
    remote_head = ref['object']['sha']
    if remote_head != parent:
        remote_tree = req(token, 'GET', f'/git/commits/{remote_head}')['tree']['sha']
        if args.force:
            print(f'⚠️ 远端 {remote_head[:8]} 与本地 parent {parent[:8]} 不同，按 --force 覆盖')
            print(f'   远端 tree {remote_tree[:8]} → 本地 tree {tree[:8]}')
            print(f'   本次改动文件: {files}')
        else:
            print(f'❌ 远端 {args.branch} 在 {remote_head[:8]}，本地父节点是 {parent[:8]}')
            print('   → 远端有本地没有的提交，先把远端拉下来再推'
                  '（内容等价可加 --force）')
            return 1

    base_tree = req(token, 'GET', f'/git/commits/{parent}')['tree']['sha']
    tree_items = []
    for path in files:
        content = subprocess.run(
            ['git', 'cat-file', 'blob', f'{commit}:{path}'],
            capture_output=True).stdout
        b64 = base64.b64encode(content).decode()
        blob = req(token, 'POST', '/git/blobs',
                   {'content': b64, 'encoding': 'base64'})
        # mode 必须取 git 里的**真实**值：可执行文件是 100755，
        # 写死 100644 会让远端 tree 与本地不一致 → commit sha 必然不同（踩过）
        mode = git('ls-files', '-s', '--', path).split()[0]
        tree_items.append({'path': path, 'mode': mode,
                           'type': 'blob', 'sha': blob['sha']})
        print(f'  blob {path} → {blob["sha"][:8]}')

    new_tree = req(token, 'POST', '/git/trees',
                   {'base_tree': base_tree, 'tree': tree_items})['sha']
    new_commit = req(token, 'POST', '/git/commits', {
        'message': message, 'tree': new_tree, 'parents': [parent],
        'author': author, 'committer': committer,
    })['sha']
    print(f'远端新 commit: {new_commit}')

    req(token, 'PATCH', f'/git/refs/heads/{args.branch}',
        {'sha': new_commit, 'force': bool(args.force)})
    print(f'✅ 远端 {args.branch} → {new_commit}')

    # 本地也重建成 UTC，让两边 sha 完全一致
    env = dict(os.environ)
    env.update({
        'GIT_AUTHOR_NAME': author['name'], 'GIT_AUTHOR_EMAIL': author['email'],
        'GIT_AUTHOR_DATE': author['date'],
        'GIT_COMMITTER_NAME': committer['name'],
        'GIT_COMMITTER_EMAIL': committer['email'],
        'GIT_COMMITTER_DATE': committer['date'],
    })
    msg_file = os.path.join(os.environ.get('TMPDIR', '/tmp'), '_apipush_msg')
    with open(msg_file, 'w', encoding='utf-8') as fh:
        fh.write(message)
    local_new = subprocess.run(
        ['git', 'commit-tree', tree, '-p', parent, '-F', msg_file],
        capture_output=True, text=True, env=env).stdout.strip()
    os.remove(msg_file)
    print(f'本地新 commit: {local_new}')

    if local_new != new_commit:
        print('⚠️ 两边 sha 仍不一致（不影响远端内容，但本地需手动对齐）')
    else:
        git('update-ref', f'refs/heads/{args.branch}', local_new, commit)
        git('update-ref', f'refs/remotes/origin/{args.branch}', local_new)
        print('✅ 本地与远端 sha 完全一致（分支已对齐）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
