# -*- coding: utf-8 -*-
"""生成「公开发布副本」：按 tools/publish_excludes.txt 排除内部资料，并重建一份新历史。

只读本仓库（**不写仓库内任何文件**），只往 --dest 指向的目录写；默认是 dry-run。

用法：
    python tools/make_publish_copy.py --dest <发布副本目录>            # 只打印计划（不写盘）
    python tools/make_publish_copy.py --dest <发布副本目录> --write    # 真复制
    python tools/make_publish_copy.py --dest <发布副本目录> --write --init-history

规则：
  · 只复制**被 git 跟踪**的文件 —— 运行数据 / 临时产物 / 截图一律带不出去；
  · 逐条套用 tools/publish_excludes.txt（`foo/` 表示整棵子树）；
  · **绝不复制 .git**：老对象（例如只被 reflog 指着的提交）不会跟着走，
    发布副本的历史由 --init-history 用新仓库重开；
  · --init-history 的提交使用集体署名，不会把本机 git 身份写进去；
  · 拒绝写到本仓库内部；--write 要求目标目录不存在或为空。

用法（在副本里复核）：
    cd <发布副本目录> && python tools/scan_sensitive.py
"""
import argparse
import os
import shutil
import subprocess
import sys

# ---- 控制台编码降级 ----
# 中文 Windows 是 GBK，英文 Windows / GitHub Actions 是 cp1252。编码装不下中文时
# 按 errors="replace" 输出 —— 别让一次 print 把脚本自己打崩（与 tests/ 里 _out() 同源）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_EXCLUDES = os.path.join(ROOT, "tools", "publish_excludes.txt")
# 公开发布副本用集体署名：不把本机 git 身份（个人姓名/邮箱）写进新历史
AUTHOR_NAME = "WBRelay contributors"
# .invalid 是 RFC 2606 保留域名，永远无法注册 —— 不会被任何 GitHub 账号认领
AUTHOR_EMAIL = "noreply@wbrelay.invalid"


def _git(args, cwd=None):
    return subprocess.run(["git"] + list(args), cwd=cwd or ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def tracked_files():
    out = _git(["ls-files", "-z"]).stdout
    return [p for p in out.split("\0") if p]


def untracked_files():
    """未跟踪且**没有被 .gitignore 忽略**的文件（多半是刚要加的新文件）。"""
    out = _git(["ls-files", "--others", "--exclude-standard", "-z"]).stdout
    return [p for p in out.split("\0") if p]


def load_excludes(path):
    """读排除清单（与 tools/scan_sensitive.py 共用同一个文件）。"""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    return [ln.strip().replace(chr(92), "/") for ln in lines
            if ln.strip() and not ln.strip().startswith("#")]


def exclude_reason(rel, excludes):
    """该文件被哪一条排除规则挡下；没被挡返回 ''。"""
    rel = rel.replace(chr(92), "/")
    for entry in excludes:
        if entry.endswith("/"):
            if rel.startswith(entry):
                return entry
        elif rel == entry:
            return entry
    return ""


def main():
    ap = argparse.ArgumentParser(description="生成公开发布副本（默认 dry-run，不写盘）")
    ap.add_argument("--dest", required=True, help="发布副本目录（不能在本仓库内部）")
    ap.add_argument("--excludes", default=DEFAULT_EXCLUDES, help="发布排除清单")
    ap.add_argument("--write", action="store_true", help="真的写盘（缺省只打印计划）")
    ap.add_argument("--init-history", action="store_true",
                    help="复制后在副本里 git init 并提交一次（集体署名）")
    ap.add_argument("--include-untracked", action="store_true",
                    help="连「未跟踪但没被 .gitignore 忽略」的文件一起复制"
                         "（默认只复制已跟踪文件；被忽略的运行数据永远不会复制）")
    ap.add_argument("--identifiers", default=os.path.join(ROOT, "tools", "release_identifiers.txt"),
                    help="发布标识清单（默认 tools/release_identifiers.txt；缺失即失败）")
    args = ap.parse_args()

    root = os.path.abspath(ROOT)
    dest = os.path.abspath(args.dest)
    if dest == root or dest.startswith(root + os.sep):
        print("[失败] 目标目录在本仓库内部：%s（会污染私有基线）" % dest)
        return 2
    if not os.path.isfile(args.excludes):
        print("[失败] 排除清单不存在：%s（fail closed，不猜默认值）" % args.excludes)
        return 2
    excludes = load_excludes(args.excludes)
    if not excludes:
        print("[失败] 排除清单为空：%s（fail closed）" % args.excludes)
        return 2

    files = tracked_files()
    if not files:
        print("[失败] 读不到 git 跟踪文件（先 git add 再跑）")
        return 2
    # 索引里可能有「已经从工作树删掉、但没 git add/rm」的条目：照抄会中途 FileNotFoundError，
    # 更糟的是可能产出一个缺文件的副本。fail closed，先让索引与工作树一致。
    missing = [rel for rel in files if not os.path.isfile(os.path.join(root, rel.replace("/", os.sep)))]
    if missing:
        print("[失败] %d 个跟踪文件在工作树里不存在（删了没 git add/rm）：" % len(missing))
        for rel in missing[:20]:
            print("  ! %s" % rel)
        if len(missing) > 20:
            print("  ! …（还有 %d 个）" % (len(missing) - 20))
        print("先 `git add -A` 让索引与工作树一致，再重跑"
              "（fail closed：不生成缺文件的发布副本）")
        return 2
    # 门禁自己的输入必须一起出去：副本里要能跑 tools/scan_sensitive.py（exit 0），
    # 而它读的就是这两个清单。未 git add 时它们不在 tracked_files 里会漏 → 副本门禁 exit 2。
    for needed in (args.excludes, args.identifiers):
        rel = os.path.relpath(os.path.abspath(needed), root).replace(os.sep, "/")
        if rel in files or exclude_reason(rel, excludes):
            continue
        if os.path.isfile(needed):
            files.append(rel)
            print("补齐门禁输入：%s（未 git add，但发布副本必须能跑扫描器）" % rel)
    untracked = untracked_files()
    if untracked and not args.include_untracked:
        print("注意：%d 个未跟踪且未被忽略的文件**不会**被复制"
              "（先 git add，或加 --include-untracked）：" % len(untracked))
        for rel in untracked[:20]:
            print("  ? %s" % rel)
        if len(untracked) > 20:
            print("  ? …（还有 %d 个）" % (len(untracked) - 20))
    if args.include_untracked and untracked:
        files = files + [p for p in untracked if p not in files]
    keep, drop = [], []
    for rel in files:
        (drop if exclude_reason(rel, excludes) else keep).append(rel)

    dirty = [ln for ln in _git(["status", "--porcelain"]).stdout.splitlines() if ln.strip()]
    print("源仓库   ：%s（%d 个跟踪文件）" % (root, len(files)))
    print("排除清单 ：%s（%d 项）" % (args.excludes, len(excludes)))
    print("发布副本 ：%s" % dest)
    print("排除掉   ：%d 个文件" % len(drop))
    for rel in drop:
        print("  - %s" % rel)
    print("将复制   ：%d 个文件（.git 不带）" % len(keep))
    if dirty:
        print("注意：工作树有 %d 处未提交改动 —— 复制的是**工作树当前内容**，"
              "不是 HEAD" % len(dirty))

    if not args.write:
        print("\n（dry-run：什么都没写。确认无误后加 --write）")
        return 0

    if os.path.exists(dest) and os.listdir(dest):
        print("[失败] 目标目录已存在且非空：%s（不覆盖已有内容）" % dest)
        return 2
    if not os.path.isdir(dest):
        os.makedirs(dest)
    copied = 0
    for rel in keep:
        src = os.path.join(root, rel.replace("/", os.sep))
        dst = os.path.join(dest, rel.replace("/", os.sep))
        parent = os.path.dirname(dst)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        shutil.copy2(src, dst)
        copied += 1
    print("已复制 %d 个文件 → %s" % (copied, dest))

    if args.init_history:
        env = dict(os.environ)
        env.update({"GIT_AUTHOR_NAME": AUTHOR_NAME, "GIT_AUTHOR_EMAIL": AUTHOR_EMAIL,
                    "GIT_COMMITTER_NAME": AUTHOR_NAME, "GIT_COMMITTER_EMAIL": AUTHOR_EMAIL})
        for cmd in (["init", "-q"], ["add", "-A"],
                    ["commit", "-q", "-m", "chore: initial public baseline"]):
            out = subprocess.run(["git"] + cmd, cwd=dest, env=env, capture_output=True,
                                 text=True, encoding="utf-8", errors="replace")
            if out.returncode != 0:
                print("[失败] git %s：%s" % (" ".join(cmd), (out.stderr or out.stdout).strip()))
                return 2
        print("已重建历史（1 个提交，集体署名 %s）" % AUTHOR_EMAIL)

    print("\n下一步：进到副本目录再跑一次 python tools/scan_sensitive.py 复核门禁")
    return 0


if __name__ == "__main__":
    sys.exit(main())
