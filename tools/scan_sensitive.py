# -*- coding: utf-8 -*-
"""只读敏感信息扫描：工作树 + Git 全历史（含 reflog 可达与悬空对象，逐条精确到 file:line）。

**只报告路径 / 行号 / 计数，绝不回显匹配到的原文** —— 报告本身不能成为新的泄漏源。

扫描项：
  CRED   疑似真实凭据：sk-<长串>、Bearer <长串>、wb_password= <字面量>
  EMAIL  邮箱地址
  PATH   **任意盘符的绝对路径**（不只是用户目录 —— 私有项目目录同样要报）：
         单反斜杠 / 双反斜杠（Python 源码转义）/ 正斜杠三种写法都命中，
         外加 /Users/<用户名>/ · /home/<用户名>/ 这类 Unix 个人目录。
         少量系统目录与测试夹具在 ALLOWED_PATH_PREFIXES 里显式放行（每条都写了理由）。
  HOST   真实域名（排除回环、保留域 .example/.invalid/.test、公开库与文档站）
  VENDOR 供应商/客户/上游标识 —— 由显式清单 `tools/release_identifiers.txt` 驱动：
         * approved                 所有者已批准（逐项 allowlist；命中仍报告，不阻断）
         * pending-owner-confirmation 未批准（命中 → 发布扫描失败，不隐藏、不静默）
         清单缺失/为空/格式错 → 直接 fail closed（退出码 2）
         清单里**只存标识原文的 SHA-256 与长度，不存明文**（清单本身会公开发布）
         比对按 `str.lower()` 折叠（大小写不敏感）：只防「原文照抄」挡不住全小写 /
         全大写这类变体；摘要一律用 `--hash` 生成（规则见 vendor_digest()）
  SCOPE  工作树扫描的文件范围 = 文本扩展名白名单（TEXT_EXT）**或无扩展名**：
         `.gitignore` / `.gitattributes` / `LICENSE` / `Dockerfile` 这类文件曾因
         `os.path.splitext()` 返回空扩展名而被整份跳过（真实漏报）。无扩展名文件先做
         NUL 探测，二进制跳过。历史扫描走 `git grep -I`，本来就没有这个洞。
  AUTHOR Git 作者身份元数据（姓名/邮箱）：**只出类型、计数与提交位置，不回显原文**
         作者邮箱未登记为 approved 时**阻断发布**（署名要所有者决定，不默认放行）
  ORPHAN 只被 reflog 指着、或已经悬空的对象（`git fsck --unreachable --no-reflogs`）：
         这类对象不在 `git log --all` 里，但仍在 .git 里可被 `git show` 取出，必须一并报

误报抑制：测试用假值（FAKE / test- / 占位 / 示例 / 变量名）与保留域名不计入。
**扫描器自身不做特殊排除**：`tools/scan_sensitive.py` 同样进文件清单（命中就报）。
标识清单只含哈希，因此也照常参与内容扫描（不再需要 skip_paths）。

用法：
    python tools/scan_sensitive.py                     # 工作树 + 全历史 + 悬空对象 + 作者元数据（发布门禁）
    python tools/scan_sensitive.py --no-history         # 只扫工作树
    python tools/scan_sensitive.py --vendor 某供应商名    # 追加未批准标识（同样会让扫描失败）
    python tools/scan_sensitive.py --identifiers <清单>
    python tools/scan_sensitive.py --hash <标识原文>      # 生成可粘贴进清单的哈希条目
    python tools/scan_sensitive.py --hash <邮箱> --author # 生成作者身份条目
发布排除清单（发布副本要整体排除哪些文件）以 `tools/publish_excludes.txt` 为唯一来源。
退出码：0=可发布信号齐全，1=有命中/未批准标识/未确认署名/残留旧对象，2=清单缺失或不可用。
"""
import argparse
import hashlib
import os
import re
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
SELF = os.path.normcase(os.path.abspath(__file__))     # 扫描器自身（也参与扫描；只用于报告里标注）
DEFAULT_IDENTIFIERS_PATH = os.path.join(ROOT, "tools", "release_identifiers.txt")
IDENTIFIER_STATUS_APPROVED = "approved"
IDENTIFIER_STATUS_PENDING = "pending-owner-confirmation"
IDENTIFIER_KIND_VENDOR = "vendor"
IDENTIFIER_KIND_AUTHOR = "author"
IDENTIFIER_KINDS = (IDENTIFIER_KIND_VENDOR, IDENTIFIER_KIND_AUTHOR)
DIGEST_PREFIX = "sha256:"
VENDOR_KIND_APPROVED = "VENDOR-APPROVED"
VENDOR_KIND_PENDING = "VENDOR-PENDING"
# 从一行里切出「可能含标识」的 token：字母数字、下划线、点、横线、斜杠、CJK
TOKEN_RE = re.compile(r"[0-9A-Za-z_\u4e00-\u9fff.\-/]+")

TEXT_EXT = {".py", ".js", ".md", ".html", ".bat", ".cmd", ".sh", ".ps1",
            ".spec", ".toml", ".json", ".yml", ".yaml", ".txt", ".cfg", ".ini"}
SKIP_DIRS = {".git", "__pycache__", "build", "dist", "node_modules", ".venv"}
# 目录名后缀同样跳过：`*.egg-info` / `*.dist-info` 是构建生成物，内容派生自
# README 与源码（**源头本身在扫描范围内**），而发布副本只拷 git 跟踪文件、
# 它们也登记在 .gitignore 里 —— 与 build/ dist/ 完全同类，不必逐字符扫。
SKIP_DIR_SUFFIXES = (".egg-info", ".dist-info")
# 二进制探测窗口：无扩展名文件只看前 8 KiB 有没有 NUL（见 _read_text_or_none）。
# `os.path.splitext(".gitignore")` 的扩展名是空串（前导点不算分隔），单靠 TEXT_EXT
# 白名单会把这类文件整份跳过 —— 真实漏报就是这么来的。
BINARY_PROBE_BYTES = 8 * 1024

# 关键词拆开拼装，避免本文件自己被扫到
_PREFIX = "s" + "k-"
PATTERNS = (
    ("CRED", re.compile(r"\b" + re.escape(_PREFIX) + r"[A-Za-z0-9_\-]{16,}")),
    ("CRED", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{20,}")),
    ("CRED", re.compile(r"(?i)\bwb_password\s*[:=]\s*[\"'][^\"']{1,}[\"']")),
    # 中文口令字段：等号两侧可能夹中文冒号 / 全角空格。
    # **只匹配「带引号的值」**：`wb_password: pwdVal` 是 JS 对象字段名（变量），不是真值，
    # 匹配无引号写法会把正常代码全扫出来；真口令一定是被引号包起来的字面量。
    ("CRED", re.compile(r"(?i)\bwb_password\s*[:=:\uFF1A]\s*[\"']+[^\"']{1,}[\"']")),
    ("CRED", re.compile(r"(?i)\badmin_token\s*[:=]\s*[\"']?[A-Za-z0-9]{16,}")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    # 任意盘符的绝对路径（不只是 C:\Users —— 私有项目目录、Python 安装路径同样要报）：
    # 盘符 + 冒号 + 紧邻的分隔符（这条要求把 `https://`、`key: value` 这类非路径写法排除掉）
    # + 至少一个目录段 + 再一个分隔符。单反斜杠 / 双反斜杠（Python 源码转义）/ 正斜杠都命中。
    ("PATH", re.compile(r"(?i)\b[A-Z]:[\\/]{1,2}[^\\/\s\"'<>()（）:：,;|`*?]{1,}[\\/]{1,2}")),
    ("PATH", re.compile(r"(?i)/(Users|home)/[A-Za-z0-9._\-]+/")),
    # UNC 网络共享路径（两个前导反斜杠 + 主机名 + 目录）：内网拓扑信息，与盘符路径同等敏感
    ("PATH", re.compile(r"\\\\[A-Za-z0-9_.\-]{1,}\\[A-Za-z0-9_.\-/]{1,}")),
    # 内网 / 链路本地 IP 段（RFC1918 + 169.254）：内网地址同样不能随发布出去
    ("IP", re.compile(r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[0-1]))\.\d{1,3}\.\d{1,3}\b")),
    ("IP", re.compile(r"\b169\.254\.\d{1,3}\.\d{1,3}\b")),
    ("HOST", re.compile(r"https?://([A-Za-z0-9.\-]+\.[A-Za-z]{2,})")),
)
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "www.w3.org", "w3.org", "openrouter.ai",
                 "pypi.org", "github.com", "raw.githubusercontent.com",
                 "docs.python.org", "schema.org", "json-schema.org",
                 # 版本更新检测的只读端点（2026-10-14 加入）：与 github.com 同源的公开 API，
                 # 产品代码里必须写全 URL，不是泄漏；见 docs/方案-版本更新检测.md
                 "api.github.com"}
# 保留域名后缀（RFC 2606 / 测试用），一律不算泄漏
RESERVED_SUFFIX = (".example", ".invalid", ".test", ".localhost", ".example.com",
                   ".local", ".internal", ".w3.org")
# 代码里正常的占位/假值标记
ALLOWED_MARKERS = ("FAKE", "fake-", "fake_", "test-", "TEST", "EXAMPLE", "xxxx", "******",
                   "占位", "示例", "${", "%s", "abcdefghijkl", "<", "curly")

# 明确无害的盘符路径前缀（**比较前会把双反斜杠归一成单反斜杠**）。新增条目必须写清理由：
# 这里只允许「系统自带目录」与「测试夹具」，绝不允许放行真实的个人目录或项目目录。
ALLOWED_PATH_PREFIXES = (
    "c:\\windows\\",        # 系统目录：测试夹具用它证明「别的 EXE 不算 WBRelay」
    "c:\\program files\\",  # 同上（Edge WebView 夹具）
)

PUBLISH_EXCLUDES_PATH = os.path.join(ROOT, "tools", "publish_excludes.txt")


def load_publish_excludes(path=None):
    """读「发布时需要排除」的清单（**唯一来源**：tools/publish_excludes.txt）。

    返回 (prefixes, error)。清单缺失 / 为空 → error（调用方 fail closed）：
    它是发布副本的输入，不能猜默认值，也不能和 README 各抄一份。
    """
    path = path or PUBLISH_EXCLUDES_PATH
    if not os.path.isfile(path):
        return (), "发布排除清单不存在：%s" % path
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except Exception as exc:
        return (), "发布排除清单不可读：%s" % exc
    out = [ln.strip().replace(chr(92), "/") for ln in lines
           if ln.strip() and not ln.strip().startswith("#")]
    if not out:
        return (), "发布排除清单为空：%s" % path
    return tuple(out), ""


# 发布排除的内部资料：记录本机接管/事故/审查证据与真实路径，不在版本控制内、不随发布。
# 注意：这里不是「忽略」—— 命中仍然逐条打印，只是不计入退出码 1。
INTERNAL_DOC_PREFIXES = load_publish_excludes()[0]


def is_internal_doc(rel_path, prefixes=None):
    """该命中位置是否属于「发布排除的内部文档」。"""
    p = str(rel_path or "").replace(chr(92), "/")
    return any(p == pre or p.startswith(pre) for pre in (prefixes or INTERNAL_DOC_PREFIXES))


def _credit_ok(text):
    return not any(t in text for t in ALLOWED_MARKERS)


def classify(text):
    """返回该行命中的类型集合（不回显原文）。"""
    kinds = set()
    for kind, pat in PATTERNS:
        for m in pat.finditer(text):
            hit = m.group(0)
            if kind == "CRED" and not _credit_ok(hit):
                continue
            if kind == "HOST":
                host = m.group(1).lower()
                if (host in ALLOWED_HOSTS or host.startswith("127.")
                        or host.endswith(RESERVED_SUFFIX)):
                    continue
            if kind == "EMAIL":
                dom = hit.rsplit("@", 1)[-1].lower()
                local = hit.rsplit("@", 1)[0].lower()
                if dom == "users.noreply.github.com":     # GitHub 机器账号（非个人邮箱）
                    continue
                if dom.endswith(RESERVED_SUFFIX) or any(
                        t in local for t in ("fake", "test", "example", "noreply", "no-reply")):
                    continue
            if kind == "PATH":
                # 占位/模板/变量不算命中 —— 两种写法都要挡：
                #   ① 占位符落在命中文本里；② 占位符正好紧跟命中之后 —— 命中可能只到
                #   最后一个分隔符就停下，后面的 `<用户名>` / `...` 不在命中文本里。
                nxt = text[m.end():m.end() + 3]
                if any(t in hit for t in ("<", ">", "...", "{", "}", "$")):
                    continue
                if nxt[:1] in ("<", "{", "$") or nxt.startswith("..."):
                    continue
                # 显式放行的系统目录（ALLOWED_PATH_PREFIXES）不算。
                if hit.replace("\\\\", "\\").lower().startswith(ALLOWED_PATH_PREFIXES):
                    continue
            kinds.add(kind)
    return kinds


def term_digest(text):
    """标识原文 / 作者邮箱的 SHA-256（小写十六进制）。"""
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def vendor_digest(term):
    """VENDOR 标识的摘要：**大小写不敏感** —— 原文与比对窗口都先 `str.lower()` 再哈希。

    只防「原文照抄」挡不住 `Vendor` / `VENDOR` / `vendor` 这类变体，而真实漏报正是一处
    全小写的遗留行。折叠规则用 `str.lower()`，与扫描器其余部分一致（作者邮箱本来就先 lower()）。
    清单里的摘要必须用 `--hash` 生成；折叠规则一旦变化，旧摘要会**静默失效**，必须同步重算。
    边界（如实说明，不假装覆盖）：非 ASCII 的特殊大小写（如土耳其语 'İ'）不做完整 casefold；
    条目标识都是 ASCII 名称，够用。窗口长度仍按**原文**字符数取。
    """
    return term_digest(str(term).lower())


def hash_entry(term, status=IDENTIFIER_STATUS_PENDING, kind=IDENTIFIER_KIND_VENDOR, note=""):
    """把一条标识原文转成清单条目（只留哈希与长度，不留明文）。"""
    term = str(term)
    digest = vendor_digest(term) if kind == IDENTIFIER_KIND_VENDOR else term_digest(term)
    return {"kind": kind, "status": status, "digest": digest,
            "length": len(term), "term": "", "note": note}


def identifier_line(entry):
    """把条目格式化成可写进清单的一行（供 --hash 输出）。"""
    extra = str(entry["length"]) if entry["kind"] == IDENTIFIER_KIND_VENDOR else "-"
    return "%s|%s|%s%s|%s|%s" % (entry["status"], entry["kind"], DIGEST_PREFIX,
                                 entry["digest"], extra, entry["note"])


def _parse_digest(payload):
    """'sha256:<hex>' → 小写 hex；不是合法摘要返回 ''。"""
    raw = str(payload or "").strip().lower()
    if not raw.startswith(DIGEST_PREFIX):
        return ""
    body = raw[len(DIGEST_PREFIX):].strip()
    if len(body) != 64 or any(c not in "0123456789abcdef" for c in body):
        return ""
    return body


def load_identifiers(path):
    """读取显式标识清单。返回 (entries, error)。

    格式：<status>|<kind>|<payload>|<extra>|<note>
      status  = approved | pending-owner-confirmation
      kind    = vendor（文本里会出现的标识名） | author（提交里的作者邮箱）
      vendor  : payload = sha256:<hex>（标识原文的 SHA-256）；extra = 字符长度
      author  : payload = sha256:<hex>（邮箱小写后的 SHA-256）；extra 忽略

    条目里**不放明文**（清单会随公开发布一起走），所以 `--hash` 是唯一的生成入口。
    **fail closed**：缺失 / 为空 / 状态非法 / kind 非法 / 摘要不是 sha256 / 长度非法 都报错。
    """
    if not path or not os.path.isfile(path):
        return [], "标识清单不存在：%s" % (path or "(未指定)")
    entries = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except Exception as exc:
        return [], "标识清单不可读：%s" % exc
    for no, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [x.strip() for x in line.split("|")]
        if len(parts) < 4:
            return [], ("标识清单第 %d 行格式错误"
                        "（应形如 <status>|<kind>|sha256:<hex>|<长度>|<note>）" % no)
        status, kind, payload, extra = parts[0], parts[1], parts[2], parts[3]
        note = parts[4] if len(parts) > 4 else ""
        if status not in (IDENTIFIER_STATUS_APPROVED, IDENTIFIER_STATUS_PENDING):
            return [], "标识清单第 %d 行状态非法：%s" % (no, status)
        if kind not in IDENTIFIER_KINDS:
            return [], "标识清单第 %d 行 kind 非法：%s（只允许 %s）" % (
                no, kind, " / ".join(IDENTIFIER_KINDS))
        digest = _parse_digest(payload)
        if not digest:
            return [], ("标识清单第 %d 行必须写 sha256:<64 位十六进制> 哈希，不接受明文；"
                        "用 python tools/scan_sensitive.py --hash <原文> 生成" % no)
        length = 0
        if kind == IDENTIFIER_KIND_VENDOR:
            try:
                length = int(extra)
            except (TypeError, ValueError):
                return [], "标识清单第 %d 行缺少有效的字符长度：%s" % (no, extra)
            if length <= 0 or length > 200:
                return [], "标识清单第 %d 行字符长度越界（1-200）：%s" % (no, extra)
        entries.append({"kind": kind, "status": status, "digest": digest,
                        "length": length, "term": "", "note": note})
    if not entries:
        return [], "标识清单为空：%s" % path
    return entries, ""


def match_identifiers(line, identifiers):
    """该行命中的标识条目。

    哈希条目用滑窗比对：标识可能长在更长的 token 内部（`<标识>.py`、`<标识>2API`），
    所以按条目登记的字符长度在 token 上取窗口算 SHA-256，而不是整 token 比对。
    比对**大小写不敏感**（两边都过 `vendor_digest()`）。
    明文条目只可能来自命令行 `--vendor`（不落盘），按大小写不敏感的子串处理。
    """
    hits = []
    for entry in identifiers:
        # 缺 kind 的条目按 vendor 处理（命令行 --vendor 与旧调用点的兼容写法）
        if entry.get("kind", IDENTIFIER_KIND_VENDOR) != IDENTIFIER_KIND_VENDOR:
            continue
        if entry.get("term"):
            if entry["term"].lower() in line.lower():
                hits.append(entry)
            continue
        n = int(entry.get("length") or 0)
        digest = entry.get("digest") or ""
        if n <= 0 or not digest:
            continue
        found = False
        for token in TOKEN_RE.findall(line):
            if len(token) < n:
                continue
            for i in range(len(token) - n + 1):
                if vendor_digest(token[i:i + n]) == digest:
                    found = True
                    break
            if found:
                break
        if found:
            hits.append(entry)
    return hits


def vendor_kind(status):
    return VENDOR_KIND_APPROVED if status == IDENTIFIER_STATUS_APPROVED else VENDOR_KIND_PENDING


def scan_text(text, where, identifiers, results):
    for i, line in enumerate(text.splitlines(), 1):
        for kind in classify(line):
            results.append((kind, where, i))
        for entry in match_identifiers(line, identifiers):
            results.append((vendor_kind(entry["status"]), where, i))


def _skip_dir(name):
    """该目录是否整棵跳过（源码/发布内容之外的生成物与缓存）。"""
    return name in SKIP_DIRS or name.endswith(SKIP_DIR_SUFFIXES)


def is_scanned_text(name):
    """工作树扫描的文件范围：文本扩展名白名单 **或 无扩展名**。

    为什么把「无扩展名」单列一类：`os.path.splitext(".gitignore")` → `('', '.gitignore')`，
    扩展名是空串，落不进 TEXT_EXT —— 这类文件（`.gitignore` / `.gitattributes` / `LICENSE` /
    `Dockerfile` 之类）曾被整份跳过，成了真实漏报。二进制另有 NUL 探测兜底。
    注意：白名单本身不扩大 —— 非文本扩展名（`.png` / `.in` 之类）依旧不扫。
    """
    ext = os.path.splitext(name)[1].lower()
    return ext in TEXT_EXT or ext == ""


def _read_text_or_none(full):
    """读文本用于扫描；二进制 / 读不动 → None（跳过）。

    先读前 BINARY_PROBE_BYTES 字节做 NUL 探测：无扩展名的可执行文件、压缩包之类
    不该被当文本读进来（白名单里的扩展名本来就都是文本）。
    """
    try:
        with open(full, "rb") as fh:
            head = fh.read(BINARY_PROBE_BYTES)
            if b"\0" in head:
                return None
            return (head + fh.read()).decode("utf-8", errors="replace")
    except OSError:
        return None


def scan_tree(identifiers, root=None):
    """扫工作树里的文本文件（扩展名白名单 + 无扩展名）。清单只含哈希，不需要 skip_paths。

    `root` 默认仓库根；测试用它指向临时目录，避免往仓库里写探针文件。
    """
    results = []
    base_root = root or ROOT
    for base, dirs, names in os.walk(base_root):
        dirs[:] = [d for d in dirs if not _skip_dir(d)]
        for n in names:
            if not is_scanned_text(n):
                continue
            full = os.path.join(base, n)
            text = _read_text_or_none(full)
            if text is None:
                continue
            rel = os.path.relpath(full, base_root)
            scan_text(text, rel, identifiers, results)
    return results


def _git(args, timeout=120, cwd=None):
    return subprocess.run(["git"] + args, cwd=cwd or ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def history_commits(cwd=None):
    """所有提交 = refs 可达 + **reflog 可达**。

    只用 `git rev-list --all` 会漏掉「被 amend/rebase 顶掉、只剩 reflog 指着」的提交 ——
    它们照旧能被 `git show` 原样取出来，所以必须一起扫（真实案例见 README 发布状态一节）。
    """
    seen, out = set(), []
    for args in (["rev-list", "--all"], ["rev-list", "--reflog", "--all"]):
        try:
            shas = _git(args, cwd=cwd).stdout.split()
        except subprocess.TimeoutExpired:
            continue
        for sha in shas:
            if sha not in seen:
                seen.add(sha)
                out.append(sha)
    return out


def scan_commit(commit, identifiers, limit=200000):
    """扫一个提交的全部文本行（一次 `git grep -e ''` → file:line 精确），只归类不回显原文。

    不再逐词条 `git grep -F <名词>`：清单里只有哈希，没有明文可以拿来当固定串搜，
    所以先取回行，再在 Python 侧用滑窗哈希比对（见 match_identifiers）。
    """
    results = []
    try:
        out = _git(["grep", "-I", "-n", "-e", "", commit, "--"], timeout=180).stdout
    except subprocess.TimeoutExpired:
        return results
    for raw in out.splitlines()[:limit]:
        parts = raw.split(":", 3)
        if len(parts) < 4:
            continue
        path, lineno, text = parts[1], parts[2], parts[3]
        try:
            line_no = int(lineno)
        except ValueError:
            continue
        where = "%s@%s:%s" % (path, commit[:6], lineno)
        for kind in classify(text):
            results.append((kind, where, line_no))
        for entry in match_identifiers(text, identifiers):
            results.append((vendor_kind(entry["status"]), where, line_no))
    return results


def scan_history(identifiers, commits=None):
    """逐提交（refs + reflog）扫描，file:line 精确。"""
    commits = list(commits) if commits is not None else history_commits()
    print("  历史提交数：%d（refs + reflog，逐提交扫描）" % len(commits))
    results = []
    for commit in commits:
        results.extend(scan_commit(commit, identifiers))
    return results


def unreachable_objects(cwd=None):
    """refs 与 reflog 都够不着、只在 .git 里躺着（或只被 reflog 指着）的对象。

    `--no-reflogs` 让 reflog 不算可达：这正是 `git gc --prune=now` 会清掉的集合。
    返回 [(type, sha)]，type ∈ commit / tree / blob。
    """
    try:
        out = _git(["fsck", "--unreachable", "--dangling", "--no-reflogs"], timeout=180, cwd=cwd)
    except subprocess.TimeoutExpired:
        return []
    items = []
    for raw in (out.stdout or "").splitlines():
        parts = raw.split()
        if len(parts) >= 3 and parts[0] in ("unreachable", "dangling"):
            items.append((parts[1], parts[2]))
    return items


def scan_unreachable(identifiers, items, skip_commits=()):
    """扫「refs 之外」对象的**内容**（计数在调用方另报）。

    已经在 history 里扫过的提交不再重复扫；没有任何新提交时才逐 blob 读内容
    （否则同一份内容会被它所属的提交与本函数各报一遍）。
    """
    skip = set(skip_commits or ())
    results = []
    fresh_commits = [sha for typ, sha in items if typ == "commit" and sha not in skip]
    for sha in fresh_commits:
        results.extend(scan_commit(sha, identifiers))
    if fresh_commits:
        return results
    for typ, sha in items:
        if typ != "blob":
            continue
        try:
            text = _git(["cat-file", "blob", sha], timeout=60).stdout
        except subprocess.TimeoutExpired:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            where = "悬空对象 %s:%d" % (sha[:7], i)
            for kind in classify(line):
                results.append((kind, where, i))
            for entry in match_identifiers(line, identifiers):
                results.append((vendor_kind(entry["status"]), where, i))
    return results


# 不算个人身份的作者名（集体署名 / 占位 / 机器账号）
NAME_SAFE = ("", "unknown", "root", "n/a", "none", "wbrelay contributors", "no-reply", "noreply")
NAME_MACHINE_TOKENS = ("bot", "ci", "actions", "automation", "runner", "jenkins",
                       "machine", "service", "noreply")


def _looks_personal_name(name):
    """除了集体署名 / 占位名 / 机器账号，其余作者名都当「疑似个人身份」计数。"""
    v = str(name or "").strip().lower()
    if v in NAME_SAFE:
        return False
    tokens = [t for t in re.split(r"[^a-z0-9]+", v) if t]
    return not any(t in NAME_MACHINE_TOKENS for t in tokens)


def _looks_personal_email(email):
    """不是已知「非个人」邮箱（机器账号 / 保留域）就计数。"""
    v = str(email or "").strip().lower()
    if not v:
        return False
    return not any(s in v for s in ("users.noreply.github.com", "noreply", "no-reply",
                                    "example.com", "example.invalid", "localhost"))


def scan_author_metadata(cwd=None, show_commits=10, approved_digests=()):
    """扫描 git 作者身份元数据（姓名 / 邮箱）。

    **只出类型、计数与提交位置（短 sha），绝不回显姓名/邮箱原文** ——
    作者邮箱本身就是个人可识别信息，不能让扫描报告变成新的泄露源。
    去重用 sha1（人名）/ sha256（邮箱，与清单里 author 条目同一算法）。

    署名是需要所有者决定的事：邮箱没在清单里登记为 approved 的提交一律计入
    `unapproved_emails`，由调用方按阻断处理（默认不替所有者点头）。
    提交范围含 reflog —— 被 amend 顶掉的提交同样会随 .git 一起被拷走。
    """
    cwd = cwd or ROOT
    approved = set(str(x).strip().lower() for x in (approved_digests or ()))
    try:
        out = _git(["log", "--reflog", "--all", "--format=%h%x09%an%x09%ae"], cwd=cwd).stdout
    except Exception as exc:
        return {"error": str(type(exc).__name__)}
    commits = 0
    names, emails = set(), set()
    personal_names = personal_emails = unapproved = 0
    locations = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        sha, name, email = parts[0], parts[1], parts[2]
        commits += 1
        names.add(hashlib.sha1(name.encode("utf-8")).hexdigest())
        emails.add(hashlib.sha1(email.lower().encode("utf-8")).hexdigest())
        pn, pe = _looks_personal_name(name), _looks_personal_email(email)
        if pn:
            personal_names += 1
        if pe:
            personal_emails += 1
        if pe and term_digest(email.strip().lower()) not in approved:
            unapproved += 1
        if (pn or pe) and len(locations) < show_commits:
            locations.append(sha)
    return {"commits": commits, "names": len(names), "emails": len(emails),
            "personal_names": personal_names, "personal_emails": personal_emails,
            "unapproved_emails": unapproved, "approved_count": len(approved),
            "locations": locations}


def summarize(label, results, show=25):
    counts = {}
    for kind, where, line in results:
        counts[kind] = counts.get(kind, 0) + 1
    print("\n=== %s ===" % label)
    if not results:
        print("  未发现")
        return counts
    print("  按类型计数：%s" % ", ".join("%s=%d" % (k, v) for k, v in sorted(counts.items())))
    seen = set()
    shown = 0
    for kind, where, line in results:
        key = (kind, where)
        if key in seen:
            continue
        seen.add(key)
        print("  [%s] %s (行 %d)" % (kind, where, line))
        shown += 1
        if shown >= show:
            print("  …（更多位置已省略；用 --show N 调大）")
            break
    return counts


def main():
    ap = argparse.ArgumentParser(description="只读敏感信息扫描（不回显原文）")
    ap.add_argument("--no-history", action="store_true", help="只扫工作树")
    ap.add_argument("--vendor", action="append", default=[],
                    help="追加未批准标识（明文，只在本次运行里生效；可多次）")
    ap.add_argument("--identifiers", default=DEFAULT_IDENTIFIERS_PATH,
                    help="显式标识清单（默认 tools/release_identifiers.txt；缺失即失败）")
    ap.add_argument("--excludes", default=PUBLISH_EXCLUDES_PATH,
                    help="发布排除清单（默认 tools/publish_excludes.txt；缺失即失败）")
    ap.add_argument("--hash", metavar="TEXT", default="",
                    help="生成一条清单条目并打印（配合 --author 生成作者条目）")
    ap.add_argument("--author", action="store_true", help="--hash 生成的是 author 条目")
    ap.add_argument("--show", type=int, default=25, help="每个分区最多列出的位置数")
    args = ap.parse_args()

    if args.hash:
        kind = IDENTIFIER_KIND_AUTHOR if args.author else IDENTIFIER_KIND_VENDOR
        text = args.hash.strip().lower() if kind == IDENTIFIER_KIND_AUTHOR else args.hash
        print(identifier_line(hash_entry(text, status=IDENTIFIER_STATUS_PENDING, kind=kind,
                                        note="待所有者确认")))
        print("（把上面这一行追加到 %s；清单里只留哈希，不留明文）" % args.identifiers)
        return 0

    excludes, ex_err = load_publish_excludes(args.excludes)
    if ex_err:
        print("[失败] %s" % ex_err)
        print("[失败] 发布排除清单是发布副本的输入：缺失/为空一律 fail closed（不猜默认值）。")
        return 2
    entries, err = load_identifiers(args.identifiers)
    if err:
        print("[失败] %s" % err)
        print("[失败] 标识清单是发布门禁的输入：缺失/为空/格式错一律 fail closed（不猜默认值）。")
        return 2
    identifiers = list(entries)
    for v in args.vendor:
        if v:
            identifiers.append(hash_entry(v, status=IDENTIFIER_STATUS_PENDING,
                                          note="命令行 --vendor 追加（未批准）"))

    print("扫描根目录：%s" % ROOT)
    print("说明：只输出类型/路径/行号/计数，不输出匹配到的原文（避免报告本身成为泄漏源）")
    print("标识清单：%s（%d 项：approved=%d / pending=%d；只存 sha256 摘要与长度，不存明文）"
          % (args.identifiers,
             len(identifiers),
             sum(1 for e in identifiers if e["status"] == IDENTIFIER_STATUS_APPROVED),
             sum(1 for e in identifiers if e["status"] != IDENTIFIER_STATUS_APPROVED)))
    for e in identifiers:
        if e["status"] != IDENTIFIER_STATUS_APPROVED:
            print("  待所有者确认：%s 条目（%s）" % (e["kind"], e["note"] or "未批准"))
    print("发布排除清单：%s（%d 项）" % (args.excludes, len(excludes)))

    skipped = sorted(SKIP_DIRS) + ["*" + s for s in SKIP_DIR_SUFFIXES]
    print("工作树范围：扩展名白名单 + 无扩展名的文本文件（二进制跳过；排除目录：%s）"
          % ", ".join(skipped))
    tree_results = scan_tree(identifiers)
    tree_counts = summarize("工作树（已跟踪 + 未跟踪）", tree_results, args.show)
    internal = [r for r in tree_results if is_internal_doc(r[1], excludes)]
    fixable = [r for r in tree_results if not is_internal_doc(r[1], excludes)]
    blocking = [r for r in fixable if r[0] != VENDOR_KIND_APPROVED]
    pending = [r for r in fixable if r[0] == VENDOR_KIND_PENDING]
    if internal:
        print("  其中属于「发布排除的内部资料」的命中：%d 条 —— 这些路径不在版本控制内，也不随发布" % len(internal))
    if fixable:
        print("  必须先处理的命中（不在内部文档排除名单里）：%d 条（其中阻断 %d 条）"
              % (len(fixable), len(blocking)))
    if pending:
        print("\n=== 发布阻断：未批准的供应商标识 ===")
        print("  %s 命中 %d 处（不在内部文档排除范围）—— 这些标识是否随发布保留，待所有者确认；"
              % (VENDOR_KIND_PENDING, len(pending)))
        print("  未批准前**不能宣称工作树干净**，也不能直接发布当前工作树或 HEAD。")
        seen_v = set()
        for kind, where, line in pending:
            if where in seen_v:
                continue
            seen_v.add(where)
            print("  [%s] %s (行 %d)" % (kind, where, line))
            if len(seen_v) >= args.show:
                print("  …（更多位置已省略；用 --show N 调大）")
                break
    hist_counts = {}
    orphan_items = []
    meta = {}
    unapproved_author = 0
    if not args.no_history:
        commits = history_commits()
        hist = scan_history(identifiers, commits)
        hist_counts = summarize("Git 全历史（refs + reflog，逐提交 file:line 精确）", hist, args.show)
        orphan_items = unreachable_objects()
        print("\n=== refs 之外的对象（reflog 可达 / 悬空）===")
        if not orphan_items:
            print("  未发现（.git 里没有 refs 之外的对象）")
        else:
            by_type = {}
            for typ, _sha in orphan_items:
                by_type[typ] = by_type.get(typ, 0) + 1
            print("  发现 %d 个：%s" % (len(orphan_items),
                  ", ".join("%s=%d" % (k, v) for k, v in sorted(by_type.items()))))
            print("  **它们不在 `git log --all` 里，但仍在 .git 里可被 `git show` 原样取出**")
            for typ, sha in orphan_items:
                if typ == "commit":
                    print("  [commit] %s" % sha[:10])
            print("  清理：git reflog expire --expire=now --expire-unreachable=now --all")
            print("        git gc --prune=now")
            orphan_hits = scan_unreachable(identifiers, orphan_items, skip_commits=commits)
            if orphan_hits:
                summarize("refs 之外对象的内容命中", orphan_hits, args.show)
        approved = [e["digest"] for e in identifiers
                    if e["kind"] == IDENTIFIER_KIND_AUTHOR
                    and e["status"] == IDENTIFIER_STATUS_APPROVED]
        meta = scan_author_metadata(approved_digests=approved)
        print("\n=== Git 作者元数据（身份信息：只出计数/位置，不回显原文）===")
        if meta.get("error"):
            print("  无法读取：%s" % meta["error"])
        else:
            unapproved_author = meta.get("unapproved_emails", 0)
            print("  提交数：%d" % meta["commits"])
            print("  作者名（去重）：%d · 作者邮箱（去重）：%d" % (meta["names"], meta["emails"]))
            print("  疑似个人身份的提交数：%d（姓名 %d / 邮箱 %d）"
                  % (max(meta["personal_names"], meta["personal_emails"]),
                     meta["personal_names"], meta["personal_emails"]))
            print("  未登记为 approved 的署名提交：%d（清单里已批准 %d 个邮箱摘要）"
                  % (unapproved_author, meta.get("approved_count", 0)))
            if meta["locations"]:
                print("  含疑似个人身份元数据的提交（最多 10 个位置）：%s" % " ".join(meta["locations"]))
            print("  说明：署名方式需所有者决定。愿意公开本人身份 → "
                  "python tools/scan_sensitive.py --hash <邮箱> --author 后把该行登记为 approved；")
            print("        不愿公开 → 重建基线时改用集体署名（见 README 发布状态）。")

    print("\n=== 清理建议 ===")
    if pending:
        print("  · 有未批准的供应商标识：所有者确认前，发布扫描保持失败（这是应然状态，不是误报）")
    if blocking:
        print("  · 工作树有必须先处理的命中：逐条确认能否换成通用占位值；真实凭据必须同时「撤销 + 删除」")
    elif internal:
        print("  · 工作树的命中全在「发布排除的内部文档」里：**不算干净**，"
              "只能靠发布副本整体排除这些文件（清单：tools/publish_excludes.txt）")
    else:
        print("  · 工作树未发现疑似凭据/邮箱/个人路径/真实域名")
    if hist_counts:
        print("  · 历史内容有命中：**改 HEAD 消除不了历史泄漏**，只能靠发布副本（新历史）整体排除")
    elif not args.no_history:
        print("  · 历史内容未发现命中")
    if orphan_items:
        print("  · .git 里还有 refs 之外的对象（reflog 可达 / 悬空）：先执行上面两条清理命令，再重跑本扫描")
    if unapproved_author:
        print("  · 署名未确认：%d 个提交的作者邮箱没在清单里登记为 approved（见上一节）" % unapproved_author)
    blocked = bool(blocking) or bool(orphan_items) or bool(unapproved_author)
    if not blocked:
        if internal:
            print("  · 发布门禁：工作树的命中全部落在发布排除清单内 —— 它们都是内部资料，"
                  "不在版本控制内、不会随仓库发布")
        else:
            print("  · 发布门禁：通过（内容 / 对象 / 署名三项都没有未决项）")
    else:
        print("  · 发布门禁：未通过 —— 本工具不改任何 Git 对象，也不删除任何文件")
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
