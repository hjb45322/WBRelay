# -*- coding: utf-8 -*-
"""日志轮转（wb.log.1 ~ wb.log.3）的隔离回归测试。

覆盖：
  L1  触发多次轮转，保留 3 代、命名固定：有 wb.log / .1 / .2 / .3，无 .4
  L2  轮转过程无行丢失 / 无串行：保留内容恰为写入历史的「连续后缀」，且每行都有时间戳前缀
  L3  并发写多线程 + 触发轮转：条数正确、无半行、无串行
  L4  只读模式零写入：READONLY=True 下调 _log，数据目录下没有任何文件
  L5  迁移兼容：预置旧命名 wb.log.old，第一次轮转后被迁成 wb.log.1（不残留 .old）
  L6  单行长度上限 / 脱敏 / 行格式不回归
  L7  2MB 触发阈值不变（默认值 + 真实阈值下确实轮转）
  L8  数据隔离自检：DATA 指向自己创建的临时目录；不碰真实 %APPDATA%

被测设计（wbrelay/log.py）：`_LOG_LOCK` 把「大小判断 → 轮转 → 追加」整段包住；
世代名固定 wb.log.1 / .2 / .3（**不带时间戳**），最多 3 代，最旧一代删除；
旧命名 wb.log.old 在第一次轮转时迁移成 wb.log.1（.1 已存在则删除 .old）。

测试手法：
  * `wbrelay.log.LOG_PATH` 是导入期常量，因此本测试 monkeypatch 它指向自建临时目录；
  * 轮转阈值通过临时调小 `LOG_ROTATE_BYTES` 快速触发多次轮转，L7 再在**真实 2MB
    阈值**下复验一次，确保默认值没被改动；
  * 「无行丢失」用**连续后缀不变式**判定：正确的轮转下，四个文件按
    wb.log.3 → .2 → .1 → wb.log 顺序读出来的内容，必须恰好是全部写入历史的最后
    N 行（既不多也不少、顺序不乱）。这个判据不依赖「第几次轮转丢弃了哪几行」的
    精确推算，因此对阈值/行长变化稳健。

测试数据全部是本脚本自己造的假日志文本，**不读取、不复制**任何真实日志；
不占用 8789。

用法：python tests/test_log_rotation.py
"""
import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import isolate

isolate.bootstrap()

from wbrelay import log as _logmod            # noqa: E402
from wbrelay.log import _log                    # noqa: E402

PASS, FAIL = [], []

# 明显的假凭据 / 假内容（绝不能是真实 Key）
FAKE_KEY = "sk-test-FAKE-KEY-ROTATE-1234567890"
LONG_TEXT = "x" * 4000

REAL_THRESHOLD = 2 * 1024 * 1024        # 产品默认阈值：不得被改动
# 测试用缩小阈值：必须**小于一条垫刀行**（见 pad()，约 434 字节），
# 否则「写完垫刀行 → 下一次 _log 触发轮转」这个前提不成立，轮转根本不会发生。
FAST_THRESHOLD = 200
CONC_THRESHOLD = 12000                  # 并发用例阈值：保证轮转发生但不足以丢代

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ")


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name, (" | " + detail) if detail else ""))


@contextlib.contextmanager
def quiet():
    """抑制 _log 的 print（测试只关心落盘，不关心控制台）。"""
    with contextlib.redirect_stdout(io.StringIO()):
        yield


def log_dir():
    return os.path.dirname(_logmod.LOG_PATH)


def names():
    try:
        return sorted(os.listdir(log_dir()))
    except OSError:
        return []


def reset_dir():
    """换一个全新的临时目录，并把 LOG_PATH 指过去。"""
    d = tempfile.mkdtemp(prefix="wbrelay-logrot-")
    _logmod.LOG_PATH = os.path.join(d, "wb.log")
    return d


def cleanup_dir(d):
    """只删自己 mkdtemp 出来、且父目录校验为系统临时目录的目录。"""
    base = os.path.basename(d.rstrip(os.sep))
    parent = os.path.abspath(os.path.dirname(os.path.abspath(d)))
    if (base.startswith("wbrelay-logrot-")
            and parent == os.path.abspath(tempfile.gettempdir())):
        shutil.rmtree(d, ignore_errors=True)


def gen_path(gen):
    return _logmod.LOG_PATH if gen == 0 else "%s.%d" % (_logmod.LOG_PATH, gen)


def read_lines(gen):
    try:
        with open(gen_path(gen), "r", encoding="utf-8") as fh:
            return fh.read().splitlines()
    except OSError:
        return []


def all_lines():
    out = []
    # 世代号越大越旧：必须从最旧一代读到最新，才能得到真实的写入顺序。
    # （曾错写成 0→KEEP，等于把日志倒过来，任何「保序」断言都会假失败。）
    for gen in range(_logmod.LOG_ROTATE_KEEP, -1, -1):
        out.extend(read_lines(gen))
    return out


def size(gen=0):
    try:
        return os.path.getsize(gen_path(gen))
    except OSError:
        return 0


def body_of(line):
    """去掉「YYYY-MM-DD HH:MM:SS 」前缀后的正文。"""
    return line[20:] if _TS_RE.match(line) else line


class Writer(object):
    """把每一行落盘的正文按顺序记下来，作为「写入历史」。"""

    def __init__(self):
        self.history = []

    def log(self, text):
        with quiet():
            _log(text)
        self.history.append(text)


def marker(rnd, i):
    return "round%d-line%02d FAKE-LOG-TEXT" % (rnd, i)


def pad(n):
    """垫刀行：足够长，一次就把当前日志推过阈值，使下一次 _log 必然轮转。"""
    return "FAKE-PAD-%04d " % n + "p" * 400


def _is_suffix_of(got, full):
    """got 是否是 full 的连续后缀（长度 <= len(full)）。"""
    if len(got) > len(full):
        return False
    return full[len(full) - len(got):] == got if got else True


def retained_suffix():
    """按「最旧→最新」读回保留内容（世代顺序），返回正文列表。"""
    out = []
    for gen in range(_logmod.LOG_ROTATE_KEEP, 0, -1):
        out.extend(body_of(ln) for ln in read_lines(gen))
    out.extend(body_of(ln) for ln in read_lines(0))
    return out


def main():
    d = reset_dir()
    print("日志目录 : %s" % log_dir())
    print("LOG_PATH : %s" % _logmod.LOG_PATH)
    print("轮转阈值 : 产品默认 %d 字节 · 保留 %d 代\n"
          % (REAL_THRESHOLD, _logmod.LOG_ROTATE_KEEP))

    try:
        # ---- L8 隔离自检 ----
        try:
            isolate.assert_isolated()
            check("L8a 隔离断言通过（DATA 指向本测试创建的临时目录）", True)
        except AssertionError as exc:
            check("L8a 隔离断言通过（DATA 指向本测试创建的临时目录）", False, str(exc))

        # ================= L1 / L2 多代轮转 + 无行丢失 =================
        _logmod.LOG_ROTATE_BYTES = FAST_THRESHOLD
        w = Writer()
        rounds = 5
        for rnd in range(rounds):
            for i in range(6):
                w.log(marker(rnd, i))
            w.log(pad(rnd))          # 把当前日志推过阈值；下一轮第一行触发轮转
        for i in range(3):
            w.log("final-line%02d" % i)

        got = names()
        for n in ("wb.log", "wb.log.1", "wb.log.2", "wb.log.3"):
            check("L1a 存在 %s" % n, n in got, repr(got))
        check("L1b 不存在 wb.log.4（最多保留 3 代）", "wb.log.4" not in got, repr(got))
        check("L1c 不存在旧命名 wb.log.old", "wb.log.old" not in got, repr(got))
        check("L1d 目录下只有这 4 个日志文件",
              got == ["wb.log", "wb.log.1", "wb.log.2", "wb.log.3"], repr(got))
        check("L1e 轮转确实发生过（wb.log 明显小于写入总量）",
              size(0) < len("".join(w.history)) // 2,
              "wb.log=%dB written=%dB" % (size(0), len("".join(w.history))))

        # ---- L2 连续后缀不变式：保留内容 == 写入历史的连续后缀 ----
        lines = all_lines()
        bad_ts = [ln[:30] for ln in lines if not _TS_RE.match(ln)]
        check("L2a 每一行都以时间戳开头（无半行）", not bad_ts, repr(bad_ts[:3]))
        retained = retained_suffix()
        check("L2b 保留内容恰为写入历史的连续后缀（无行丢失 / 无串行 / 顺序正确）",
              retained == w.history[-len(retained):],
              "retained=%d history=%d" % (len(retained), len(w.history)))
        check("L2c 有内容被滚出（多代轮转真实发生）",
              0 < len(w.history) - len(retained),
              "discarded=%d" % (len(w.history) - len(retained)))
        check("L2d 保留内容无重复行（轮转不会复制内容）",
              len(retained) == len(set(retained)),
              "lines=%d uniq=%d" % (len(retained), len(set(retained))))
        # 自己写进去的标记行，只要还在保留范围内就必须完整无缺
        keep_from = len(w.history) - len(retained)
        expected_kept = w.history[keep_from:]
        check("L2e 保留区间内的每一行内容都与写入时逐字节一致",
              retained == expected_kept,
              "retained=%d expected=%d" % (len(retained), len(expected_kept)))

        # ================= L3 并发写 + 触发轮转 =================
        d2 = reset_dir()
        cleanup_dir(d)
        d = d2
        _logmod.LOG_ROTATE_BYTES = CONC_THRESHOLD
        WRITERS, PER = 8, 60
        want = WRITERS * PER

        want_order = ["T%02d-i%03d" % (k, i) for k in range(WRITERS) for i in range(PER)]

        def worker(wid):
            for i in range(PER):
                with quiet():
                    _log("T%02d-i%03d" % (wid, i))

        threads = [threading.Thread(target=worker, args=(k,)) for k in range(WRITERS)]
        for t in threads:
            t.start()
        # 并发期间由主线程垫刀，逼出轮转（与写线程竞争同一把 _LOG_LOCK）
        for n in range(6):
            time.sleep(0.01)
            with quiet():
                _log(pad(900 + n))
        for t in threads:
            t.join()

        got = names()
        lines = all_lines()
        bad_ts = [ln[:40] for ln in lines if not _TS_RE.match(ln)]
        tagged = [body_of(ln) for ln in lines if _TS_RE.match(ln)]
        tagged = [t for t in tagged if re.match(r"^T\d{2}-i\d{3}$", t)]
        want_set = set("T%02d-i%03d" % (k, i) for k in range(WRITERS) for i in range(PER))
        # 轮转语义：保留是有界的（最多 LOG_ROTATE_KEEP 代），所以「全部都在」在这里不成立；
        # 成立的是：保留内容无重复 / 无半行 / 无串行，且是写入历史的连续后缀。
        # 「并发无丢行」另由下面的 L3i 用**不触发轮转**的阈值单独证明。
        check("L3a 并发写下无半行（全部以时间戳开头）", not bad_ts, repr(bad_ts[:3]))
        check("L3b 并发写下每行结构完整（无串行拼接）",
              set(tagged) <= want_set and all(re.match(r"^T\d{2}-i\d{3}$", t) for t in tagged),
              "unexpected=%d" % len(set(tagged) - want_set))
        check("L3c 并发写下无重复行", len(set(tagged)) == len(tagged),
              "uniq=%d tagged=%d" % (len(tagged), len(set(tagged))))
        # 并发下**全局顺序不确定**（8 个线程交错），但每个线程自己的行必须保序。
        # 轮转只丢最旧的：所以每个线程保留的行必须是它自己历史的连续后缀。
        check("L3d 每个线程保留的行都是自己历史的连续后缀（轮转只丢最旧的）",
              all(_is_suffix_of([t for t in tagged if t.startswith("T%02d-" % k)],
                                ["T%02d-i%03d" % (k, i) for i in range(PER)])
                  for k in range(WRITERS)),
              "tagged=%d" % len(tagged))
        check("L3e 并发写下轮转确实发生过", "wb.log.1" in got, repr(got))
        check("L3f 并发写下世代数未超限（无 wb.log.4）", "wb.log.4" not in got, repr(got))
        check("L3g 并发写下目录下只有 4 个以内的日志文件",
              all(n == "wb.log" or re.match(r"^wb\.log\.[123]$", n) for n in got), repr(got))

        # ---- L3i 并发无丢行：同样并发量，阈值设为产品默认（本轮不轮转）----
        d2b = reset_dir()
        cleanup_dir(d)
        d = d2b
        _logmod.LOG_ROTATE_BYTES = REAL_THRESHOLD

        def worker_b(wid):
            for i in range(PER):
                with quiet():
                    _log("T%02d-i%03d" % (wid, i))

        tb = [threading.Thread(target=worker_b, args=(k,)) for k in range(WRITERS)]
        for t in tb:
            t.start()
        for t in tb:
            t.join()
        lines_b = all_lines()
        tagged_b = [body_of(ln) for ln in lines_b if _TS_RE.match(ln)]
        tagged_b = [t for t in tagged_b if re.match(r"^T\d{2}-i\d{3}$", t)]
        check("L3i 并发写下无丢行（不触发轮转时每条都在）",
              len(tagged_b) == want and set(tagged_b) == want_set,
              "tagged=%d want=%d" % (len(tagged_b), want))
        check("L3j 并发写下无重复行（不触发轮转）",
              len(set(tagged_b)) == len(tagged_b),
              "uniq=%d tagged=%d" % (len(set(tagged_b)), len(tagged_b)))
        check("L3k 不触发轮转时确实只有 wb.log", names() == ["wb.log"], repr(names()))

        # ================= L5 迁移兼容：旧命名 wb.log.old =================
        d3 = reset_dir()
        cleanup_dir(d)
        d = d3
        _logmod.LOG_ROTATE_BYTES = FAST_THRESHOLD
        with open(_logmod.LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write("legacy-current-line FAKE-OLD-1\n")
        with open(_logmod.LOG_PATH + ".old", "a", encoding="utf-8") as fh:
            fh.write("legacy-old-generation FAKE-OLD-2\n")
        check("L5a 预置旧命名 wb.log.old 成功", "wb.log.old" in names(), repr(names()))
        w = Writer()
        w.log(pad(0))                       # 推过阈值
        w.log("after-legacy-migration")     # 这一次触发轮转 → 迁移 .old
        got = names()
        check("L5b 轮转后旧命名被迁移（wb.log.old 消失）", "wb.log.old" not in got, repr(got))
        # 迁移后 .old 变成第 1 代；随后当前日志滚下来时它再后移到第 2 代，
        # 因此断言「内容仍在某一代里」而不是钉死某一个世代号。
        check("L5c 旧内容被保留（迁移而非丢弃，世代内可后移）",
              any("legacy-old-generation FAKE-OLD-2" in ln
                  for g in range(1, _logmod.LOG_ROTATE_KEEP + 1) for ln in read_lines(g)),
              repr([read_lines(g)[:1] for g in range(1, _logmod.LOG_ROTATE_KEEP + 1)]))
        check("L5d 迁移后仍能正常追加到 wb.log",
              any("after-legacy-migration" in ln for ln in read_lines(0)),
              repr(read_lines(0)[-2:]))

        # ---- L5b 旧命名 + 已有 .1：旧文件更老，直接删除 ----
        d4 = reset_dir()
        cleanup_dir(d)
        d = d4
        _logmod.LOG_ROTATE_BYTES = FAST_THRESHOLD
        with open(gen_path(1), "a", encoding="utf-8") as fh:
            fh.write("gen1-newer FAKE-GEN1\n")
        with open(_logmod.LOG_PATH + ".old", "a", encoding="utf-8") as fh:
            fh.write("older FAKE-OLD\n")
        w = Writer()
        w.log(pad(0))
        w.log("trigger-rotate")
        got = names()
        check("L5e 已有 .1 时旧命名被删除（不残留永不清理的历史）",
              "wb.log.old" not in got, repr(got))
        check("L5f 世代数仍未超限", "wb.log.4" not in got, repr(got))
        check("L5g 世代 2 已形成", "wb.log.2" in got, repr(got))
        check("L5h 新世代 1 是刚滚下来的内容",
              any("gen1-newer FAKE-GEN1" in ln for ln in read_lines(2)),
              repr(read_lines(2)[:2]))

        # ================= L6 单行上限 / 脱敏 / 行格式 =================
        d5 = reset_dir()
        cleanup_dir(d)
        d = d5
        with quiet():
            _log("leak test " + FAKE_KEY)
            _log(LONG_TEXT)
        lines = read_lines(0)
        check("L6a 单行长度上限仍生效（LOG_LINE_LIMIT == 800）",
              _logmod.LOG_LINE_LIMIT == 800 and len(lines[1]) < 800 + 40,
              "len=%d" % len(lines[1]))
        check("L6b 脱敏仍生效（假 Key 被掩码，不落原文）",
              FAKE_KEY not in lines[0] and "sk-******" in lines[0], repr(lines[0][:80]))
        check("L6c 行格式仍是「YYYY-MM-DD HH:MM:SS <text>」",
              _TS_RE.match(lines[0]) is not None and lines[0][19] == " ",
              repr(lines[0][:25]))

        # ================= L7 真实 2MB 阈值 =================
        d6 = reset_dir()
        cleanup_dir(d)
        d = d6
        _logmod.LOG_ROTATE_BYTES = REAL_THRESHOLD
        check("L7a 恢复产品默认阈值 2MB", _logmod.LOG_ROTATE_BYTES == REAL_THRESHOLD,
              repr(_logmod.LOG_ROTATE_BYTES))
        w = Writer()
        # 用真实 _log 把当前日志推过 2MB（每行约 450 字节，约 4700 行）
        while size(0) <= REAL_THRESHOLD:
            w.log(pad(7000 + len(w.history)))
        w.log("after-real-threshold-rotate")       # 这一次触发轮转
        got = names()
        lines = all_lines()
        check("L7b 真实 2MB 阈值下确实发生了轮转", "wb.log.1" in got, repr(got))
        check("L7c 真实阈值下世代数未超限", "wb.log.4" not in got, repr(got))
        check("L7d 真实阈值下所有行仍完整（时间戳前缀）",
              all(_TS_RE.match(ln) for ln in lines),
              repr([ln[:30] for ln in lines if not _TS_RE.match(ln)][:3]))
        check("L7e 真实阈值下轮转后新行落在 wb.log",
              any("after-real-threshold-rotate" in ln for ln in read_lines(0)),
              repr(read_lines(0)[-2:]))
        retained = retained_suffix()
        check("L7f 真实阈值下保留内容仍是写入历史的连续后缀",
              retained == w.history[-len(retained):],
              "retained=%d history=%d" % (len(retained), len(w.history)))

        # ================= L4 只读模式零写入（子进程）=================
        _run_readonly_child()

        # ================= L8 收尾 =================
        try:
            isolate.assert_isolated()
            check("L8b 收尾隔离自检通过", True)
        except AssertionError as exc:
            check("L8b 收尾隔离自检通过", False, str(exc))
    finally:
        cleanup_dir(d)

    print("\n===== 日志轮转：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  失败项：" + f)
    return 0 if not FAIL else 1


def _run_readonly_child():
    """只读模式必须在独立子进程里验证（READONLY 在 paths 导入期定死）。

    断言：READONLY=True 下调 _log，数据目录下没有任何文件（连目录都不创建）。
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = (
        "import contextlib, io, os, sys, tempfile\n"
        "os.environ['WBRELAY_READONLY'] = '1'\n"
        "root = tempfile.mkdtemp(prefix='wbrelay-ro-')\n"
        "os.environ['APPDATA'] = root\n"
        "sys.path.insert(0, %r)\n"
        "from wbrelay import log as L\n"
        "L.LOG_PATH = os.path.join(root, 'data', 'wb.log')\n"
        "buf = io.StringIO()\n"
        "with contextlib.redirect_stdout(buf):\n"
        "    L._log('readonly probe FAKE-ROTATE')\n"
        "    L._log('readonly probe 2 ' + 'y' * 900)\n"
        "data = os.path.join(root, 'data')\n"
        "files = []\n"
        "for base, dirs, ns in os.walk(data):\n"
        "    files += [os.path.relpath(os.path.join(base, n), data) for n in ns]\n"
        "print('RO_FILES=' + repr(sorted(files)))\n"
        "print('RO_DIR_EXISTS=' + repr(os.path.exists(data)))\n"
        "print('RO_READONLY=' + repr(L.READONLY))\n"
    ) % repo
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    for k in ("WBRELAY_DATA_DIR", "WBRELAY_READONLY", "WBRELAY_UPSTREAM_DIR",
              "WBRELAY_NO_IDE_INJECT", "WBRELAY_HEADLESS"):
        env.pop(k, None)
    proc = subprocess.run([sys.executable, "-c", code], env=env, cwd=repo,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
    out = proc.stdout.decode("utf-8", "replace")
    for line in out.splitlines():
        print("  [child] " + line[:120])
    check("L4a 只读子进程正常退出", proc.returncode == 0, "exit=%s" % proc.returncode)
    check("L4b 只读模式确实生效", "RO_READONLY=True" in out, out[-200:])
    check("L4c 只读模式下零写入（数据目录无任何文件、连目录都没创建）",
          "RO_FILES=[]" in out and "RO_DIR_EXISTS=False" in out, out[-200:])
    check("L4d 只读子进程没有 traceback", "Traceback" not in out, out[-200:])


if __name__ == "__main__":
    sys.exit(main())
