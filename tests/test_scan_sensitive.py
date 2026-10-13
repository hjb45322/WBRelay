# -*- coding: utf-8 -*-
"""敏感信息扫描器的回归测试。

背景（两次真实漏报）：
  1. Windows 用户路径的正则只能匹配「双反斜杠」写法，常见的单反斜杠写法完全漏扫，
     报告因此把工作树说成「干净」；
  2. 只认 `C:\\Users\\...` 这一种形状 —— 私有项目目录（别的盘符、别的目录）照样漏，
     而且历史扫描只走 `git rev-list --all`，被 amend 顶掉、只剩 reflog 的提交看不见
     （它们仍在 .git 里能被 `git show` 取出）。

覆盖：
  C1  单反斜杠 / 双反斜杠（Python 源码转义）/ 正斜杠 / Unix 个人目录 → 都命中 PATH
  C2  占位写法（<name> / ... / $变量）与系统目录夹具、保留域名、假凭据不计入
  C3  扫描器自身不再被排除：自己的源码逐行过 classify() 不得命中
  C4  作者元数据：只报计数/位置，返回值里不得出现姓名/邮箱原文
  C5  「发布排除的内部资料」识别（反斜杠路径也要认得出来）
  C6  显式标识清单：fail closed、未批准 → 阻断、已批准 → 只报告
  C7  清单里只留哈希：条目可生成、可解析、可按窗口匹配更长 token
  C8  发布排除清单是唯一来源（tools/publish_excludes.txt）
  C9  refs 之外的对象：reflog 可达的提交与悬空对象都能被发现
  C10 署名门禁：作者邮箱未登记为 approved → 阻断；登记后放行

隔离：测试只读仓库文件，临时 git 仓库建在本测试自己的临时根里。

用法：python tests/test_scan_sensitive.py
"""
import importlib.util
import io
import os
import subprocess
import sys
import tempfile

import isolate

TMP_ROOT = isolate.bootstrap()
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL = [], []
B = chr(92)
Q = chr(34)
SQ = chr(39)
PROBE_PWD = "secret" + "1234"       # 探针值：拼出来，别让测试文件本身被扫到
PROBE_HOST = "api." + "openai" + ".com"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name, (" | " + detail) if detail else ""))


def _p(*parts):
    """把路径片段拼起来：测试文件文本里不出现「看上去像真实个人路径」的字面量。"""
    return "".join(parts)


def load_scanner():
    """按路径导入扫描器（不修改它的行为，也不把 tools/ 加进 sys.path）。"""
    path = os.path.join(REPO, "tools", "scan_sensitive.py")
    spec = importlib.util.spec_from_file_location("wbrelay_scan_sensitive_probe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    sc = load_scanner()
    check("C0a 扫描器宜导入且指向仓库根", sc.ROOT == REPO, sc.ROOT)

    # ---- C1 三种写法都必须命中 ----
    samples = [
        ("反斜杠（单层，文件里的真实形式）",
         "p = " + _p("C:", B, "Users", B, "alice", B, "notes.txt")),
        ("双反斜杠（Python 源码转义）",
         "p = " + _p('"C:', B*2, "Users", B*2, "bob", B*2, 'x.txt"')),
        ("正斜杠", "url = " + _p("C:", "/Users", "/carol", "/docs")),
        ("正斜杠（双重）", "url = " + _p("C:", "//Users", "//dave", "//docs")),
        ("Unix 个人目录", "path = " + _p("/Users", "/erin", "/project")),
        ("Unix /home", "path = " + _p("/home", "/frank", "/project")),
        ("UNC 网络共享", "share = " + Q + B * 2 + "nas01" + B + "backup" + Q),
    ]
    for label, text in samples:
        check("C1 命中 PATH：%s" % label, "PATH" in sc.classify(text), text[:20])

    # 新增 IP 类型：内网地址同样是泄漏项
    ip_cases = [
        ("192.168", "host = 'http://%d.%d.1.50:8788/v1'" % (192, 168)),
        ("10 段", "addr = %d.%d.%d.%d" % (10, 20, 30, 40)),
        ("172.16", "gw = %d.%d.5.9" % (172, 16)),
        ("169.254", "link = %d.%d.1.1" % (169, 254)),
    ]
    for label, text in ip_cases:
        check("C1i 命中 IP：%s" % label, "IP" in sc.classify(text), text[:20])

    # 中文口令字段没有引号也要命中
    check("C1j 命中 CRED：中文冒号口令字段（无引号）",
          "CRED" in sc.classify("wb_password ： " + SQ + PROBE_PWD + SQ))
    # 反过来：JS 对象字段名 / 变量不能算命中（否则看板与测试全被扫出来）
    check("C1j2 不误报：wb_password: pwdVal 是字段名不是值",
          not sc.classify("    wb_password: pwdVal"))
    check("C1j3 不误报：var body = { wb_password: pwdVal }",
          not sc.classify("var body = { wb_password: pwdVal };"))

    # 不误报：公网域名 / 回环 / 版本号
    ip_neg = [
        ("公网域名", "api = 'https://" + PROBE_HOST + "/v1'"),
        ("回环地址", "url = 'http://127.0.0.1:8789/v1'"),
        ("普通版本号", "ver = '1.2.3.4'"),
    ]
    for label, text in ip_neg:
        check("C1k 不误报：%s" % label, "IP" not in sc.classify(text), text[:20])

    # ---- C2 占位/保留域/假凭据不计入 ----
    negatives = [
        ("占位路径", _p("C:", B, "Users", B, "<name>", B, "x")),
        ("省略号路径", "C:" + B + "Users" + B + "..." + B + "x"),
        ("保留域名", "http://example.invalid/v1"),
        ("假凭据", "api_key = sk-test-FAKE-KEY-0000"),
    ]
    for label, text in negatives:
        check("C2 不命中：%s" % label, not sc.classify(text), repr(text))

    # ---- C3 自身不排除：扫描器源码逐行不命中 ----
    src = io.open(os.path.join(REPO, "tools", "scan_sensitive.py"), encoding="utf-8").read()
    self_hits = [(i, sorted(sc.classify(line)))
                 for i, line in enumerate(src.splitlines(), 1) if sc.classify(line)]
    check("C3a 扫描器自身不再从文件清单里排除（旧的 SELF 跳过代码已删除）",
          "os.path.abspath(full)) == SELF" not in src and "SELF" in src, "src-len=%d" % len(src))
    check("C3b 扫描器源码自身逐行不命中（实例/作用域都不会自匹配）",
          not self_hits, repr(self_hits[:3]))

    # ---- C4 作者元数据（临时 git 仓库）----
    repo = tempfile.mkdtemp(prefix="wbrelay-scan-git-")
    try:
        # 不设 GIT_AUTHOR_* 环境变量：否则它会盖掉 `-c user.name/user.email`，两个提交会变同一个作者
        env = dict(os.environ)
        env.update({"HOME": repo, "USERPROFILE": repo})
        run = lambda *a: subprocess.run(["git"] + list(a), cwd=repo, env=env,
                                        capture_output=True, text=True, timeout=60)
        run("init", "-q")
        with io.open(os.path.join(repo, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write("hello\n")
        run("add", "f.txt")
        run("-c", "user.name=" + PERSONAL_NAME, "-c", "user.email=" + PERSONAL_EMAIL,
            "-c", "commit.gpgsign=false", "commit", "-q", "-m", "first")
        with io.open(os.path.join(repo, "f.txt"), "a", encoding="utf-8") as fh:
            fh.write("machine\n")
        run("add", "f.txt")
        run("-c", "user.name=ci-bot", "-c", "user.email=ci-bot@users.noreply.github.com",
            "-c", "commit.gpgsign=false", "commit", "-q", "-m", "second")
        meta = sc.scan_author_metadata(cwd=repo)
        check("C4a 作者元数据能读到两个提交", meta.get("commits") == 2, repr(meta))
        check("C4b 个人邮箱计数为 1（noreply 机器账号不计）", meta.get("personal_emails") == 1,
              repr(meta.get("personal_emails")))
        check("C4c 个人姓名计数为 1", meta.get("personal_names") == 1,
              repr(meta.get("personal_names")))
        check("C4d 去重后各有两个作者名/邮箱", meta.get("names") == 2 and meta.get("emails") == 2,
              repr(meta))
        check("C4e 只报位置且不回显原文",
              bool(meta.get("locations")) and 7 <= len(meta["locations"][0]) <= 12
              and PERSONAL_EMAIL not in repr(meta) and PERSONAL_NAME not in repr(meta),
              repr(meta.get("locations")))
    finally:
        if os.path.basename(repo).startswith("wbrelay-scan-git-") and \
                os.path.normcase(os.path.dirname(os.path.abspath(repo))) == \
                os.path.normcase(os.path.abspath(tempfile.gettempdir())):
            __import__("shutil").rmtree(repo, ignore_errors=True)

    # ---- C6 显式标识清单（第三轮：发布门禁输入）----
    ids_path = os.path.join(REPO, "tools", "release_identifiers.txt")
    entries, err = sc.load_identifiers(ids_path)
    check("C6a 显式标识清单可读且非空", not err and len(entries) >= 2, err or ("%d 项" % len(entries)))
    check("C6b 清单项带 approved / pending 状态",
          all(e["status"] in (sc.IDENTIFIER_STATUS_APPROVED, sc.IDENTIFIER_STATUS_PENDING)
              for e in entries),
          repr(sorted(set(e["status"] for e in entries))))
    missing, err = sc.load_identifiers(os.path.join(REPO, "tools", "no-such-identifiers.txt"))
    check("C6c 清单缺失 → fail closed（返回错误、无条目）", bool(err) and missing == [], err)
    bad_file = os.path.join(isolate.tmp_root(), "bad-identifiers.txt")
    with io.open(bad_file, "w", encoding="utf-8") as fh:
        fh.write("bogus-status|term|note\n")
    bad, err = sc.load_identifiers(bad_file)
    check("C6d 清单状态非法 → fail closed", bool(err) and bad == [], err)
    empty_file = os.path.join(isolate.tmp_root(), "empty-identifiers.txt")
    with io.open(empty_file, "w", encoding="utf-8") as fh:
        fh.write("# 只有注释\n")
    empty, err = sc.load_identifiers(empty_file)
    check("C6e 清单为空 → fail closed", bool(err) and empty == [], err)
    results = []
    sc.scan_text("probe-term appears here", "probe.txt",
                 [{"term": "probe-term", "status": sc.IDENTIFIER_STATUS_PENDING, "note": ""},
                  {"term": "approved-term", "status": sc.IDENTIFIER_STATUS_APPROVED, "note": ""}],
                 results)
    check("C6f 未批准标识 → VENDOR-PENDING（会让扫描失败）",
          results == [(sc.VENDOR_KIND_PENDING, "probe.txt", 1)], repr(results))
    results = []
    sc.scan_text("approved-term here", "probe.txt",
                 [{"term": "approved-term", "status": sc.IDENTIFIER_STATUS_APPROVED, "note": ""}],
                 results)
    check("C6g 已批准标识 → VENDOR-APPROVED（仍报告，但不阻断）",
          results == [(sc.VENDOR_KIND_APPROVED, "probe.txt", 1)], repr(results))
    src = io.open(os.path.join(REPO, "tools", "scan_sensitive.py"), encoding="utf-8").read()
    check("C6h 扫描器不再需要 skip_paths（清单只留哈希，可照常参与扫描）",
          "skip_paths=" not in src)
    ids_raw = io.open(ids_path, encoding="utf-8").read()
    check("C6i 清单里只有 sha256 摘要，没有任何明文标识",
          "sha256:" in ids_raw and all(len(e["digest"]) == 64 for e in entries))

    # ---- C7 哈希条目：生成 / 解析 / 窗口匹配 ----
    entry = sc.hash_entry("probe-term")
    check("C7a 生成的条目里不含明文", "probe-term" not in sc.identifier_line(entry),
          sc.identifier_line(entry)[:40] + "...")
    check("C7b 条目记录了字符长度", entry["length"] == len("probe-term"))
    check("C7c 精确出现 → 命中", len(sc.match_identifiers("x = 'probe-term'", [entry])) == 1)
    check("C7d 长在更长 token 里也命中（probe-term.py / probe-termXYZ）",
          len(sc.match_identifiers("see probe-term.py here", [entry])) == 1
          and len(sc.match_identifiers("probe-termXYZ", [entry])) == 1)
    check("C7e 无关文本不命中", sc.match_identifiers("nothing to see here", [entry]) == [])
    rt = os.path.join(isolate.tmp_root(), "roundtrip-identifiers.txt")
    with io.open(rt, "w", encoding="utf-8") as fh:
        fh.write(sc.identifier_line(entry) + "\n")
    back, err = sc.load_identifiers(rt)
    check("C7f 生成的条目能被读回同一摘要",
          not err and len(back) == 1 and back[0]["digest"] == entry["digest"], err)
    plain = os.path.join(isolate.tmp_root(), "plaintext-identifiers.txt")
    with io.open(plain, "w", encoding="utf-8") as fh:
        fh.write("pending-owner-confirmation|vendor|probe-term|10|\n")
    bad, err = sc.load_identifiers(plain)
    check("C7g 清单里写明文 → fail closed", bool(err) and bad == [], err)
    noauth = os.path.join(isolate.tmp_root(), "badauthor-identifiers.txt")
    with io.open(noauth, "w", encoding="utf-8") as fh:
        fh.write("approved|author|sha256:" + "0" * 64 + "|-\n")
    ok_author, err = sc.load_identifiers(noauth)
    check("C7h author 条目可解析（extra 忽略）", not err and len(ok_author) == 1, err)

    # ---- C8 发布排除清单是唯一来源 ----
    ex, err = sc.load_publish_excludes()
    check("C8a 发布排除清单可读且非空", not err and len(ex) >= 1, err or ("%d 项" % len(ex)))
    check("C8b 内部资料目录在排除清单里", "_internal" in " ".join(ex), repr(ex[:2]))
    check("C8c is_internal_doc 接受传入清单（唯一来源，不再另抄一份）",
          sc.is_internal_doc("_internal/docs/x.md", ex) is True
          and sc.is_internal_doc("README.md", ex) is False)
    check("C8d 排除清单缺失 → fail closed（不猜默认值）",
          sc.load_publish_excludes(os.path.join(REPO, "tools", "no-such-excludes.txt"))[1] != "")

    # ---- C9 refs 之外的对象（reflog 可达 / 悬空）----
    repo2 = tempfile.mkdtemp(prefix="wbrelay-scan-orphan-")
    try:
        env2 = dict(os.environ)
        env2.update({"HOME": repo2, "USERPROFILE": repo2})
        target = os.path.join(repo2, "a.txt")

        def git2(*a):
            return subprocess.run(["git"] + list(a), cwd=repo2, env=env2,
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace")

        git2("init", "-q")
        with io.open(target, "w", encoding="utf-8") as fh:
            fh.write("first\n")
        git2("add", "-A")
        git2("-c", "user.name=Probe", "-c", "user.email=" + PERSONAL_EMAIL,
             "commit", "-q", "-m", "first")
        with io.open(target, "w", encoding="utf-8") as fh:
            fh.write("second\n")
        git2("add", "-A")
        git2("-c", "user.name=Probe", "-c", "user.email=" + PERSONAL_EMAIL,
             "commit", "-q", "--amend", "-m", "first (amended)")
        refs_only = set(git2("rev-list", "--all").stdout.split())
        with_reflog = set(sc.history_commits(cwd=repo2))
        check("C9a 只走 refs 会漏掉被 amend 顶掉的提交",
              len(with_reflog) > len(refs_only),
              "reflog=%d refs=%d" % (len(with_reflog), len(refs_only)))
        items = sc.unreachable_objects(cwd=repo2)
        check("C9b refs 之外的对象能被发现", bool(items),
              repr(sorted(set(t for t, _ in items))))
        check("C9c 其中包含提交对象", any(t == "commit" for t, _ in items))

        # ---- C10 署名门禁 ----
        before = sc.scan_author_metadata(cwd=repo2)
        after = sc.scan_author_metadata(
            cwd=repo2, approved_digests=[sc.term_digest(PERSONAL_EMAIL.lower())])
        check("C10a 未登记的署名 → 计入 unapproved_emails（阻断用）",
              before.get("unapproved_emails", 0) >= 1,
              repr(before.get("unapproved_emails")))
        check("C10b 登记为 approved 后不再计入（署名由所有者明确确认）",
              after.get("unapproved_emails", 0) == 0, repr(after.get("unapproved_emails")))
    finally:
        if os.path.basename(repo2).startswith("wbrelay-scan-orphan-") and \
                os.path.normcase(os.path.dirname(os.path.abspath(repo2))) == \
                os.path.normcase(os.path.abspath(tempfile.gettempdir())):
            __import__("shutil").rmtree(repo2, ignore_errors=True)

    # ---- C5 内部文档识别 ----
    check("C5a 反斜杠路径能认出内部资料",
          sc.is_internal_doc("_internal" + B + "docs" + B + "\u4e8b\u6545\u8bb0\u5f55.md") is True)
    check("C5b 正斜杠路径也能认出来",
          sc.is_internal_doc("_internal/docs/\u4e8b\u6545\u8bb0\u5f55.md") is True)
    check("C5c 普通发布文件不算内部资料", sc.is_internal_doc("README.md") is False)
    check("C5d 内部资料名册非空（前缀至少一条）",
          len(sc.INTERNAL_DOC_PREFIXES) >= 1)

    print("\n===== 敏感信息扫描器：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  失败项：" + f)
    return 0 if not FAIL else 1


# 用拼接构造：文件文本里不出现「看得出是真实邮箱」的字面值（扫描器只看文本）
PERSONAL_EMAIL = "alice.person" + chr(64) + "corp" + ".local"
PERSONAL_NAME = "Alice " + "Person"


if __name__ == "__main__":
    sys.exit(main())
