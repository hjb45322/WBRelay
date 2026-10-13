# -*- coding: utf-8 -*-
"""数据目录路径策略（隔离启动器工具，第三轮 R1/R6 加固）。

职责：把用户给的 `--data-dir` 规范化为**绝对路径**，并把每一层已存在的组件用
`os.path.realpath` 解析（Windows 上能解析 junction / symlink），然后判定它是否落在
受保护范围里：

  * `%APPDATA%` 整棵树（因此覆盖 `%APPDATA%\\WBRelay`、`WBRelay-OSS`、`WBRelay-legacy-*`）
  * 仓库目录（含子目录）
  * `%USERPROFILE%\\.codex`（含子目录）
  * 显式 `--forbidden`（或环境变量 `WBRELAY_FORBIDDEN_PATHS`，`;` 分隔）里的任何路径
    —— 原项目 / 上游项目的真实路径属敏感信息，不写进仓库，由所有者显式注入。

只做路径字符串与 reparse point 解析，不读文件内容、不列目录（`os.path.exists` / `realpath`
只沿给定的祖先链探测）。判定失败返回非 0，调用方（`tools/run-isolated.ps1`）必须拒绝启动。

用法（给启动器调用）：
    python tools/data_path_policy.py --data-dir <路径> --repo-root <仓库根>
    → stdout 最后一行是规范化绝对路径；exit 0=放行，3=拒绝，2=参数错误
"""
import argparse
import os
import sys


def canonical(path):
    """字面规范化：绝对路径 + normpath + normcase（不做 reparse 解析）。"""
    if not path:
        return ""
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def resolve_reparse(path):
    """逐层解析已存在组件的真实路径（junction / symlink）。不存在的尾巴按字面保留。"""
    if not path:
        return ""
    p = os.path.abspath(path)
    drive, tail = os.path.splitdrive(p)
    if drive:
        resolved = drive + os.sep
    else:
        resolved = os.sep
    parts = [x for x in tail.replace("/", os.sep).split(os.sep) if x]
    for part in parts:
        candidate = os.path.join(resolved, part)
        if os.path.exists(candidate):
            try:
                resolved = os.path.realpath(candidate)
            except OSError:
                resolved = candidate
        else:
            resolved = candidate
    return os.path.normcase(os.path.normpath(resolved))


def is_within(child, parent):
    """child 是否等于 parent 或位于 parent 之内（大小写不敏感、分隔符边界正确）。"""
    c, p = canonical(child), canonical(parent)
    if not c or not p:
        return False
    if c == p:
        return True
    return c.startswith(p.rstrip("\\/") + os.sep)


def _hit(path, root, label):
    """字面路径与 reparse 解析结果都要判（Windows 8.3 短名 / junction 都要覆盖）。"""
    if not root:
        return None
    root_resolved = resolve_reparse(root)
    for child in (path, resolve_reparse(path)):
        for parent in (root, root_resolved):
            if is_within(child, parent):
                return "%s：%s" % (label, path)
    return None


def check_data_dir(data_dir, repo_root, appdata_root=None, user_profile=None, forbidden=()):
    """返回 (resolved_abs_path, problems[])。problems 非空表示必须拒绝。"""
    if not data_dir:
        return "", ["未指定数据目录"]
    literal = os.path.abspath(data_dir)
    resolved = resolve_reparse(literal)
    problems = []
    roots = [
        (repo_root, "数据目录不能位于本项目仓库内"),
        (appdata_root, "数据目录不能位于 %APPDATA% 内（覆盖 WBRelay / WBRelay-OSS / WBRelay-legacy-*）"),
        (os.path.join(user_profile, ".codex") if user_profile else None,
         "数据目录不能位于 ~\\.codex 内"),
    ]
    for root, label in roots:
        for p in (literal, resolved):
            hit = _hit(p, root, label)
            if hit:
                problems.append(hit)
    for extra in forbidden:
        if not extra:
            continue
        for p in (literal, resolved):
            hit = _hit(p, extra, "数据目录命中显式禁止路径（原项目/上游等）")
            if hit:
                problems.append(hit)
    # 去重但保序
    seen, out = set(), []
    for p in problems:
        key = os.path.normcase(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return resolved, out


def main(argv=None):
    ap = argparse.ArgumentParser(description="隔离启动器的数据目录路径策略（只读判定）")
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--repo-root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--appdata", default=os.environ.get("APPDATA") or "")
    ap.add_argument("--user-profile", default=os.environ.get("USERPROFILE") or "")
    ap.add_argument("--forbidden", action="append", default=[],
                    help="额外禁止的路径（可多次；环境变量 WBRELAY_FORBIDDEN_PATHS 用 ; 分隔）")
    args = ap.parse_args(argv)

    forbidden = list(args.forbidden)
    env_extra = os.environ.get("WBRELAY_FORBIDDEN_PATHS") or ""
    forbidden.extend([p.strip() for p in env_extra.split(";") if p.strip()])

    resolved, problems = check_data_dir(args.data_dir, args.repo_root,
                                        appdata_root=args.appdata,
                                        user_profile=args.user_profile,
                                        forbidden=forbidden)
    if problems:
        for p in problems:
            sys.stderr.write("[拒绝] %s\n" % p)
        sys.stderr.write("[拒绝] 请换一个独立临时目录（例如 %%TEMP%%\\wbrelay-dev）；"
                         "隔离启动器不会自动顺延、也不会修改受保护数据。\n")
        return 3
    sys.stdout.write(resolved + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())