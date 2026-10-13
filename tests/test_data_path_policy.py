# -*- coding: utf-8 -*-
"""数据目录路径策略（tools/data_path_policy.py）的隔离测试（第三轮）。

覆盖：路径规范化 / 大小写 / 尾部分隔符 / 相对路径 / junction 解析 / CLI 退出码。
隔离：所有「受保护目录」都是本测试在临时根里自建的**假**目录；
      测试不读、不写、不枚举真实 %APPDATA%、~\.codex、仓库或原项目/上游目录。

用法：python tests/test_data_path_policy.py
"""
import importlib.util
import io
import os
import subprocess
import sys
import tempfile

import isolate

isolate.bootstrap()          # 独立临时 APPDATA/LOCALAPPDATA/USERPROFILE/HOME + 独立临时根

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLICY = os.path.join(REPO, "tools", "data_path_policy.py")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name, (" | " + detail) if detail else ""))


def load_policy():
    spec = importlib.util.spec_from_file_location("wbrelay_data_path_policy_probe", POLICY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    mod = load_policy()
    tmp = tempfile.mkdtemp(prefix="wbrelay-path-policy-")
    fake_appdata = os.path.join(tmp, "appdata")
    fake_profile = os.path.join(tmp, "profile")
    fake_repo = os.path.join(tmp, "repo")
    fake_forbidden = os.path.join(tmp, "original-project")
    fake_ok = os.path.join(tmp, "temp-data")
    for d in (fake_appdata, os.path.join(fake_profile, ".codex"), fake_repo, fake_forbidden, fake_ok):
        os.makedirs(d, exist_ok=True)

    def check_dir(path, **kw):
        resolved, problems = mod.check_data_dir(
            path, repo_root=fake_repo, appdata_root=fake_appdata,
            user_profile=fake_profile, forbidden=[fake_forbidden], **kw)
        return resolved, problems

    try:
        check("C0 策略脚本可导入且指向仓库根",
              os.path.dirname(os.path.dirname(POLICY)) == REPO)

        # ---- 允许：临时目录 ----
        resolved, problems = check_dir(fake_ok)
        check("C1 独立临时目录 → 放行", not problems, "；".join(problems))
        check("C1b 返回的是规范化绝对路径（reparse 解析后的真实路径）",
              os.path.isabs(resolved) and
              os.path.normcase(resolved) == os.path.normcase(mod.resolve_reparse(fake_ok)),
              resolved)

        # ---- 受保护目录：大小写 / 尾部分隔符 / 子目录 ----
        cases = [
            ("%APPDATA%\\WBRelay（本项目数据目录）", os.path.join(fake_appdata, "WBRelay")),
            ("%APPDATA%\\WBRelay-OSS（历史）", os.path.join(fake_appdata, "WBRelay-OSS")),
            ("%APPDATA%\\WBRelay-legacy-20260101（归档）", os.path.join(fake_appdata, "WBRelay-legacy-20260101")),
            ("%APPDATA% 下的任意目录（超集保护）", os.path.join(fake_appdata, "anything")),
            ("仓库目录本身", fake_repo),
            ("仓库子目录", os.path.join(fake_repo, "sub", "data")),
            ("~/.codex", os.path.join(fake_profile, ".codex")),
            ("~/.codex/model-catalogs 子目录", os.path.join(fake_profile, ".codex", "model-catalogs")),
            ("显式禁止路径（原项目）", os.path.join(fake_forbidden, "data")),
        ]
        for label, path in cases:
            _resolved, problems = check_dir(path)
            check("C2 拒绝 %s" % label, bool(problems), "；".join(problems)[:120])

        # ---- 大小写 / 尾部分隔符 ----
        upper = os.path.join(fake_appdata.upper(), "WBRelay".upper(), "")
        _r, problems = check_dir(upper)
        check("C3 大小写不敏感 + 尾部分隔符 → 仍拒绝", bool(problems), "；".join(problems)[:120])
        _r, problems = check_dir(os.path.join(fake_ok, ".", "sub", ".."))
        check("C3b 路径里的 . / .. 被规范化后放行", not problems, "；".join(problems))

        # ---- 相对路径 ----
        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            _r, problems = check_dir(os.path.join("appdata", "WBRelay"))
            check("C4 相对路径（落在假 APPDATA 内）→ 拒绝", bool(problems), "；".join(problems)[:120])
            _r, problems = check_dir("temp-data")
            check("C4b 相对路径（独立临时目录）→ 放行", not problems, "；".join(problems))
        finally:
            os.chdir(old_cwd)

        # ---- junction 解析（临时目录 + 临时 junction；不碰真实受保护目录）----
        link = os.path.join(tmp, "link-to-appdata")
        created = subprocess.run(["cmd", "/c", "mklink", "/J", link, fake_appdata],
                                 capture_output=True, text=True, errors="replace")
        if created.returncode == 0 and os.path.exists(link):
            _r, problems = check_dir(link)
            check("C5 junction 指向假 %APPDATA% → 拒绝", bool(problems), "；".join(problems)[:120])
            _r, problems = check_dir(os.path.join(link, "WBRelay", "nested"))
            check("C5b junction 下的子路径 → 拒绝", bool(problems), "；".join(problems)[:120])
            link_ok = os.path.join(tmp, "link-to-ok")
            made_ok = subprocess.run(["cmd", "/c", "mklink", "/J", link_ok, fake_ok],
                                     capture_output=True, text=True, errors="replace")
            if made_ok.returncode == 0 and os.path.exists(link_ok):
                resolved, problems = check_dir(os.path.join(link_ok, "sub"))
                check("C5c 指向非受保护目录的 junction → 放行且返回解析后的真实路径",
                      not problems and
                      os.path.normcase(os.path.dirname(resolved)) ==
                      os.path.normcase(mod.resolve_reparse(fake_ok)),
                      resolved)
                try:
                    os.rmdir(link_ok)
                except OSError:
                    pass
            else:
                check("C5c 指向非受保护目录的 junction（跳过：无法创建）", True)
            try:
                os.rmdir(link)      # 只删 junction 本身，不删目标
            except OSError:
                pass
        else:
            check("C5 junction 测试（跳过：本机不允许创建 junction）",
                  True, (created.stdout or created.stderr or "").strip()[:80])

        # ---- CLI 退出码 ----
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        ok = subprocess.run([sys.executable, POLICY, "--data-dir", fake_ok,
                             "--repo-root", fake_repo, "--appdata", fake_appdata,
                             "--user-profile", fake_profile,
                             "--forbidden", fake_forbidden],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", env=env)
        check("C6 CLI 放行 → exit 0 且 stdout 是绝对路径",
              ok.returncode == 0 and os.path.isabs((ok.stdout or "").strip()),
              "rc=%s out=%r" % (ok.returncode, (ok.stdout or "").strip()[:80]))
        bad = subprocess.run([sys.executable, POLICY, "--data-dir",
                              os.path.join(fake_appdata, "WBRelay-legacy-x"),
                              "--repo-root", fake_repo, "--appdata", fake_appdata,
                              "--user-profile", fake_profile],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", env=env)
        check("C6b CLI 命中归档目录 → exit 3 且 stderr 说明原因",
              bad.returncode == 3 and "拒绝" in (bad.stderr or ""),
              "rc=%s err=%r" % (bad.returncode, (bad.stderr or "")[:80]))
        bad2 = subprocess.run([sys.executable, POLICY, "--data-dir", fake_repo,
                               "--repo-root", fake_repo, "--appdata", fake_appdata,
                               "--user-profile", fake_profile],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env)
        check("C6c CLI 命中仓库目录 → exit 3", bad2.returncode == 3, "rc=%s" % bad2.returncode)
        # ---- C7 启动器静态核对（PowerShell 脚本本身不启动任何实例）----
        launcher = io.open(os.path.join(REPO, "tools", "run-isolated.ps1"),
                           encoding="utf-8").read()
        check("C7a 启动器调用 data_path_policy.py 做路径判定",
              "data_path_policy.py" in launcher)
        check("C7b 先做路径判定与端口检查，再创建数据目录",
              launcher.find("data_path_policy.py") < launcher.find("New-Item -ItemType Directory")
              and launcher.find("Test-PortFree") < launcher.find("New-Item -ItemType Directory"))
        check("C7c 显式拒绝 8789", '$Port -eq 8789' in launcher)
        check("C7d 端口占用时退出（不自动顺延、不杀进程）",
              "Test-PortFree" in launcher and
              "不自动顺延" in launcher and "taskkill" not in launcher.lower())
        check("C7e 不再有 SeedFromLive 参数/用法（不复制在用配置）", "$SeedFromLive" not in launcher)
        check("C7f 用策略返回的规范路径启动",
              "--data-dir', $resolvedDataDir" in launcher or
              "data-dir', $resolvedDataDir" in launcher)
    finally:
        if os.path.basename(tmp).startswith("wbrelay-path-policy-") and \
                os.path.normcase(os.path.dirname(os.path.abspath(tmp))) == \
                os.path.normcase(os.path.abspath(tempfile.gettempdir())):
            __import__("shutil").rmtree(tmp, ignore_errors=True)

    print("\n===== 数据目录路径策略：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  失败项：" + f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())