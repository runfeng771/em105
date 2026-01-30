import os
import re
import time
import base64
import threading
from datetime import datetime, timedelta, timezone
from collections import deque, OrderedDict

import requests
import ddddocr
from flask import Flask, request, jsonify, render_template_string, send_from_directory, make_response
from apscheduler.schedulers.background import BackgroundScheduler
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_v1_5

# =========================
# Flask
# =========================
app = Flask(__name__)

# =========================
# 静态背景图（放在 ./static/bg/ 下）
# - 你的 Render 项目里把背景图提交到仓库即可
# - 前端每次刷新随机切换；Service Worker + Cache-Control 做“本地缓存”
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BG_DIR = os.getenv("BG_DIR", os.path.join(BASE_DIR, "pic"))
BG_DIR = os.path.abspath(BG_DIR)

# =========================
# Health / Keepalive
# - /api/health: 监控/保活入口
# - 20秒自检：后端自己请求一次 /api/health（用于验证调度器和HTTP栈都正常）
# 说明：Render Free 是否休眠取决于“外部入站请求”，自检不等同于保活，
#      但能帮你在日志里确认调度器一直在跑。
# =========================
HEALTH_PATH = "/api/health"
# 优先使用 Render 提供的外部域名；你也可以手动设置 HEALTH_BASE_URL（例如自定义域名）
HEALTH_BASE_URL = (os.getenv("HEALTH_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip()
HEALTH_BASE_URL = HEALTH_BASE_URL.rstrip("/")

HEALTH_LOCK = threading.Lock()
HEALTH_STATE = {
    "last_heartbeat_epoch": 0.0,
    "last_heartbeat_ok": False,
    "last_heartbeat_err": "",
    "resolved_base_url": HEALTH_BASE_URL,  # 运行时可能会被首次请求动态补全
}

def _set_health(**kwargs):
    with HEALTH_LOCK:
        HEALTH_STATE.update(kwargs)

def _get_health():
    with HEALTH_LOCK:
        return dict(HEALTH_STATE)

def resolve_base_url_from_request():
    """按 em10 的思路：优先 env，其次用请求的 host_url 动态补全一次。"""
    if HEALTH_BASE_URL:
        return HEALTH_BASE_URL
    # request.host_url 形如 "https://xxx.onrender.com/"
    try:
        base = request.host_url.rstrip("/")
        _set_health(resolved_base_url=base)
        return base
    except Exception:
        return _get_health().get("resolved_base_url") or ""

def list_bg_files():
    if not os.path.isdir(BG_DIR):
        return []
    exts = {".jpg", ".jpeg", ".png", ".webp"}
    files = []
    for name in os.listdir(BG_DIR):
        p = os.path.join(BG_DIR, name)
        if os.path.isfile(p) and os.path.splitext(name.lower())[1] in exts:
            files.append(name)
    files.sort()
    return files


def bg_url_for(name: str):
    # 用 mtime 做版本号，保证更新后能刷新缓存
    try:
        mtime = int(os.path.getmtime(os.path.join(BG_DIR, name)))
    except Exception:
        mtime = int(time.time())
    return f"/bg/{name}?v={mtime}"


# =========================
# 固定接口
# =========================
CMS_UNLOCK_URL = "https://cmsapi3.qiucheng-wangluo.com/cms-api/club/unlockClubManager"
CMS_CLUBINFO_URL = "https://cmsapi3.qiucheng-wangluo.com/cms-api/club/clubInfo"
CMS_USER_LOOKUP_URL = "https://cmsapi3.qiucheng-wangluo.com/cms-api/user/getSpecifyUserByShowId"

CMS_REFERER = "https://cms.ayybyyy.com/"
CLUB_ID = 104137139  # 固定 clubId（你提供的）

# =========================
# 账号密码：Render 用环境变量覆盖
# =========================
DEFAULT_ACCOUNT = "tbh2356@126.com"
DEFAULT_PASSWORD = "112233qq"
CMS_ACCOUNT = os.getenv("CMS_ACCOUNT", DEFAULT_ACCOUNT)
CMS_PASSWORD = os.getenv("CMS_PASSWORD", DEFAULT_PASSWORD)

# =========================
# 日志缓冲（前端展示）
# =========================
LOG_LOCK = threading.Lock()
LOG_BUF = deque(maxlen=200)


def _push_line(line: str):
    with LOG_LOCK:
        LOG_BUF.appendleft(line)


def log_blank():
    _push_line("")


def log_sep(title: str):
    _push_line("────────────────────────────────────────")
    _push_line(f"【{title}】{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    _push_line("────────────────────────────────────────")


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    _push_line(f"[{ts}] {msg}")


def clear_logs():
    with LOG_LOCK:
        LOG_BUF.clear()


# =========================
# Token 缓存：每次登录成功覆盖为最新
# =========================
TOKEN_LOCK = threading.Lock()
TOKEN_CACHE = {
    "token": None,
    "last_login_at": None,
    "last_login_ok": False,
    "last_login_err": "",
}


def set_token(token: str):
    with TOKEN_LOCK:
        TOKEN_CACHE["token"] = token
        TOKEN_CACHE["last_login_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        TOKEN_CACHE["last_login_ok"] = True
        TOKEN_CACHE["last_login_err"] = ""


def set_login_fail(err: str):
    with TOKEN_LOCK:
        TOKEN_CACHE["last_login_ok"] = False
        TOKEN_CACHE["last_login_err"] = err


def get_token():
    with TOKEN_LOCK:
        return TOKEN_CACHE["token"]


def get_status_snapshot():
    with TOKEN_LOCK:
        return dict(TOKEN_CACHE)


# =========================
# CLUB 上下文缓存：clubInfo 是否成功（用于判断是否需要重登）
# =========================
CLUBCTX_LOCK = threading.Lock()
CLUBCTX_CACHE = {
    "ok": False,
    "last_at": None,
    "last_err": "",
    "last_resp": None,
}


def set_clubctx_ok(resp):
    with CLUBCTX_LOCK:
        CLUBCTX_CACHE["ok"] = True
        CLUBCTX_CACHE["last_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        CLUBCTX_CACHE["last_err"] = ""
        CLUBCTX_CACHE["last_resp"] = resp


def set_clubctx_fail(err: str, resp=None):
    with CLUBCTX_LOCK:
        CLUBCTX_CACHE["ok"] = False
        CLUBCTX_CACHE["last_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        CLUBCTX_CACHE["last_err"] = err
        CLUBCTX_CACHE["last_resp"] = resp


def get_clubctx():
    with CLUBCTX_LOCK:
        return dict(CLUBCTX_CACHE)


# =========================
# 玩家资料缓存（showid -> {showid, uuid, strNick, strCover, cached_at}）
# 说明：内存缓存，Render 重启会丢失（符合你现阶段诉求）
# =========================
USERCACHE_LOCK = threading.Lock()
USERCACHE_MAX = 200
USERCACHE = OrderedDict()  # showid -> dict


def cache_user(profile: dict):
    """
    profile: {showid, uuid, strNick, strCover}
    """
    showid = str(profile.get("showid") or "").strip()
    if not showid:
        return
    with USERCACHE_LOCK:
        # 最近使用置顶
        if showid in USERCACHE:
            del USERCACHE[showid]
        USERCACHE[showid] = {
            "showid": showid,
            "uuid": profile.get("uuid"),
            "strNick": profile.get("strNick") or "",
            "strCover": profile.get("strCover") or "",
            "cached_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        while len(USERCACHE) > USERCACHE_MAX:
            USERCACHE.popitem(last=False)


def list_cached_users():
    with USERCACHE_LOCK:
        # 最新在前
        return list(reversed(list(USERCACHE.values())))


def clear_user_cache():
    with USERCACHE_LOCK:
        USERCACHE.clear()


def cache_count():
    with USERCACHE_LOCK:
        return len(USERCACHE)


# =========================
# 登录器（整合你脚本核心流程）
# =========================
class CMSAutoLogin:
    def __init__(self):
        self.session = requests.Session()
        self.ocr = ddddocr.DdddOcr()
        self.max_attempts = 5

        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": CMS_REFERER
        }

        # 固定公钥（第一次加密用）
        self.first_public_key = "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDNR7I+SpqIZM5w3Aw4lrUlhrs7VurKbeViYXNhOfIgP/4acsWvJy5dPb/FejzUiv2cAiz5As2DJEQYEM10LvnmpnKx9Dq+QDo7WXnT6H2szRtX/8Q56Rlzp9bJMlZy7/i0xevlDrWZMWqx2IK3ZhO9+0nPu4z4SLXaoQGIrs7JxwIDAQAB"

    def get_captcha_token(self):
        url = "https://cmsapi3.qiucheng-wangluo.com/cms-api/token/generateCaptchaToken"
        r = self.session.post(url, headers=self.headers, timeout=15)
        r.raise_for_status()
        j = r.json()
        if j.get("iErrCode") != 0:
            raise RuntimeError(f"generateCaptchaToken失败: {j.get('sErrMsg')}")
        return j.get("result")

    def get_captcha_img_b64(self, captcha_token: str):
        url = "https://cmsapi3.qiucheng-wangluo.com/cms-api/captcha"
        r = self.session.post(url, headers=self.headers, data={"token": captcha_token}, timeout=15)
        r.raise_for_status()
        j = r.json()
        if j.get("iErrCode") != 0:
            raise RuntimeError(f"captcha失败: {j.get('sErrMsg')}")
        return j.get("result")

    def recognize_captcha(self, captcha_base64: str) -> str:
        img = base64.b64decode(captcha_base64)
        txt = self.ocr.classification(img)
        txt = re.sub(r"[^a-zA-Z0-9]", "", txt)
        if len(txt) > 4:
            txt = txt[:4]
        return txt.upper()

    def load_public_key(self, key_str: str):
        try:
            if "-----BEGIN" in key_str:
                return RSA.import_key(key_str)
            try:
                der_data = base64.b64decode(key_str)
                return RSA.import_key(der_data)
            except Exception:
                try:
                    hex_str = re.sub(r"\s+", "", key_str)
                    if len(hex_str) % 2 != 0:
                        hex_str = "0" + hex_str
                    der_data = bytes.fromhex(hex_str)
                    return RSA.import_key(der_data)
                except Exception:
                    return RSA.import_key(key_str)
        except Exception as e:
            raise RuntimeError(f"加载公钥失败: {e}")

    def rsa_encrypt_long(self, text: str, public_key_str: str) -> str:
        public_key = self.load_public_key(public_key_str)
        key_size = public_key.n.bit_length() // 8
        max_block_size = key_size - 11
        encrypted_blocks = []
        for i in range(0, len(text), max_block_size):
            block = text[i:i + max_block_size]
            cipher = PKCS1_v1_5.new(public_key)
            encrypted_blocks.append(cipher.encrypt(block.encode("utf-8")))
        return base64.b64encode(b"".join(encrypted_blocks)).decode("utf-8")

    def login(self, account, password, captcha, captcha_token):
        url = "https://cmsapi3.qiucheng-wangluo.com/cms-api/login"

        first_encrypted_password = self.rsa_encrypt_long(password, self.first_public_key)
        second_encrypted_password = self.rsa_encrypt_long(first_encrypted_password, captcha_token)
        encrypted_account = self.rsa_encrypt_long(account, captcha_token)

        data = {
            "account": encrypted_account,
            "data": second_encrypted_password,
            "safeCode": captcha,
            "token": captcha_token,
            "locale": "zh",
        }

        r = self.session.post(url, headers=self.headers, data=data, timeout=20)
        r.raise_for_status()
        return r.json()

    def login_and_get_token(self, account: str, password: str) -> str:
        for attempt in range(1, self.max_attempts + 1):
            try:
                log(f"INFO  登录尝试 {attempt}/{self.max_attempts}")

                captcha_token = self.get_captcha_token()
                log(f"INFO  captcha_token 获取成功: {captcha_token[:22]}...")

                img_b64 = self.get_captcha_img_b64(captcha_token)
                captcha_text = self.recognize_captcha(img_b64)
                if not captcha_text or len(captcha_text) != 4:
                    raise RuntimeError(f"OCR验证码异常: {captcha_text}")
                log(f"INFO  OCR验证码: {captcha_text}")

                login_result = self.login(account, password, captcha_text, captcha_token)
                if login_result.get("iErrCode") != 0:
                    raise RuntimeError(f"login失败: {login_result.get('sErrMsg', '未知错误')}")

                token = login_result.get("result")
                if not token:
                    raise RuntimeError("login成功但 result 为空（未返回 token）")

                log("SUCCESS 登录成功：获得 token（完整如下）")
                log(token)
                return token

            except Exception as e:
                log(f"ERROR 本次登录失败: {e}")
                if attempt < self.max_attempts:
                    time.sleep(2 ** attempt)

        raise RuntimeError("达到最大重试次数，登录失败")


login_client = CMSAutoLogin()


# =========================
# clubInfo：登录后必须先调用一次（对齐你提供的 fetch）
# 同时写入 CLUBCTX_CACHE
# =========================
def fetch_club_info_with_token(token: str, club_id: int = CLUB_ID):
    headers = {
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "token": token,
        "referer": CMS_REFERER,
        "user-agent": "Mozilla/5.0",
    }
    data = {"clubId": str(club_id)}

    log_sep("CLUB CONTEXT (clubInfo)")
    log("INFO  clubInfo 使用 token（完整如下）")
    log(token)
    log(f"INFO  clubInfo 请求: clubId={club_id}")

    r = requests.post(CMS_CLUBINFO_URL, headers=headers, data=data, timeout=15)
    log(f"INFO  clubInfo 响应: status={r.status_code}")
    log(f"INFO  clubInfo body: {r.text}")

    try:
        r.raise_for_status()
    except Exception as e:
        set_clubctx_fail(f"http_error: {e}", resp=r.text)
        raise

    try:
        j = r.json()
    except Exception:
        j = {"raw": r.text}

    if isinstance(j, dict) and j.get("iErrCode") == 0:
        set_clubctx_ok(j)
        log("SUCCESS clubInfo iErrCode=0 ✅ 上下文建立成功")
    else:
        set_clubctx_fail("clubInfo iErrCode != 0", resp=j)
        log(f"WARNING clubInfo 上下文未建立/失败: {j}")

    return j


# =========================
# 确保 token + 上下文可用（查询/解封统一走这一套）
# =========================
def ensure_auth_and_context() -> tuple[bool, str]:
    """
    return (ok, msg)
    ok True：token 存在且 clubctx_ok=True
    """
    token = get_token()
    ctx = get_clubctx()

    if token and ctx.get("ok"):
        return True, "ok"

    log_sep("AUTH/CTX NOT READY -> AUTO LOGIN")
    if not token:
        log("WARNING token 缓存为空，触发 refresh_token_once() ...")
    else:
        log(f"WARNING token 已有但上下文未建立（last_err={ctx.get('last_err')}），触发 refresh_token_once() ...")

    ok, msg = refresh_token_once(source="manual", bump_schedule=True)
    return ok, msg


# =========================
# 玩家查询：getSpecifyUserByShowId（固定 clubId + token）
# =========================
def fetch_user_by_showid(showid: str, token: str):
    """
    按你提供的 fetch 格式构造：
    URL 固定，body: showId=<输入>&clubId=<固定>
    """
    headers = {
        "accept": "application/json, text/javascript, */*; q=0.01",
        "accept-language": "zh",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "token": token,
        "referer": CMS_REFERER,
        "user-agent": "Mozilla/5.0",
    }
    data = {
        "showId": str(showid).strip(),
        "clubId": str(CLUB_ID),
    }

    log_sep("USER LOOKUP (getSpecifyUserByShowId)")
    log(f"INFO  查询 showid={showid} clubId={CLUB_ID}")
    log("INFO  查询请求 token（完整如下）")
    log(token)

    r = requests.post(CMS_USER_LOOKUP_URL, headers=headers, data=data, timeout=12)
    log(f"INFO  查询响应: status={r.status_code}")
    log(f"INFO  查询 body: {r.text}")

    r.raise_for_status()
    try:
        j = r.json()
    except Exception:
        j = {"raw": r.text}

    return j


# =========================
# APScheduler：每 3 分钟自动登录一次
# =========================
scheduler = BackgroundScheduler(
    timezone="UTC",
    job_defaults={
        # coalesce: 合并错过的触发，只执行一次
        "coalesce": True,
        # 防止并发重入（登录过程可能较慢）
        "max_instances": 1,
        # Render/系统暂停或卡顿后，允许在较大窗口内补跑
        "misfire_grace_time": 6 * 60 * 60,
    },
)
LOGIN_INTERVAL_MIN = int(os.getenv("LOGIN_INTERVAL_MIN", "3"))
LOGIN_JOB_ID = f"login_{LOGIN_INTERVAL_MIN}min"
WATCHDOG_JOB_ID = "login_watchdog_1min"

# 自动登录任务监控（用于判定“是否漏跑”）
MON_LOCK = threading.Lock()
MON = {
    "last_login_start_epoch": 0.0,
    "last_login_end_epoch": 0.0,
    "last_login_source": "",
    "missed_count": 0,
}


def _set_mon(**kwargs):
    with MON_LOCK:
        MON.update(kwargs)


def _get_mon():
    with MON_LOCK:
        return dict(MON)


def refresh_token_once(source: str = "manual", bump_schedule: bool = True):
    """
    登录刷新：更新缓存最新 token + 立刻调用 clubInfo 建立上下文
    如果 clubInfo 未成功，则自动“重走一次登录流程”（只重试 1 次，避免死循环）
    """
    # 记录本次登录触发来源 + 时间戳（用于 watchdog 判断是否漏跑）
    _set_mon(last_login_start_epoch=time.time(), last_login_source=source)
    try:
        for round_i in (1, 2):
            log_blank()
            log_sep("LOGIN CYCLE" if round_i == 1 else "CONTEXT RETRY (RE-LOGIN)")

            log("INFO  开始执行登录刷新 token ...")
            token = login_client.login_and_get_token(CMS_ACCOUNT, CMS_PASSWORD)

            # 1) 缓存最新 token
            set_token(token)
            cached = get_token()

            log_sep("TOKEN CHECK")
            log("INFO  登录获取 token（完整如下）")
            log(token)
            log("INFO  缓存 token（完整如下）")
            log(cached)

            if cached != token:
                log("WARNING 缓存 token 与登录 token 不一致！后续将以缓存为准")
            else:
                log("SUCCESS 缓存 token 与登录 token 一致 ✅")

            # 2) 必须先调用 clubInfo（用最新 token）
            club_info = fetch_club_info_with_token(cached, CLUB_ID)

            # 3) 成功则结束
            if isinstance(club_info, dict) and club_info.get("iErrCode") == 0:
                _set_mon(last_login_end_epoch=time.time())
                if bump_schedule:
                    bump_next_login_run(LOGIN_INTERVAL_MIN)
                return True, "ok"

            # 4) 失败：第一次失败则重登一次；第二次还失败则退出
            if round_i == 1:
                log("WARNING clubInfo 未成功，准备重走一次登录流程以建立上下文 ...")
                time.sleep(1.2)
                continue

            err = f"clubInfo 上下文建立失败（已重登1次仍失败），返回: {club_info}"
            set_login_fail(err)
            _set_mon(last_login_end_epoch=time.time())
            _set_mon(last_login_end_epoch=time.time())
            return False, err

    except Exception as e:
        set_login_fail(str(e))
        log_sep("LOGIN FAILED")
        log(f"ERROR token 刷新失败: {e}")
        _set_mon(last_login_end_epoch=time.time())
        return False, str(e)


def bump_next_login_run(minutes: int = None):
    """把下一次自动登录的 next_run_time 强制设为“现在 + minutes”，让倒计时与实际登录时间对齐。"""
    if minutes is None:
        minutes = LOGIN_INTERVAL_MIN
    try:
        job = scheduler.get_job(LOGIN_JOB_ID)
        if not job:
            return
        # APScheduler 使用 UTC（我们配置了 timezone=UTC）
        next_dt = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        scheduler.modify_job(LOGIN_JOB_ID, next_run_time=next_dt)
        log(f"INFO  next autologin 重新对齐为: {next_dt.strftime('%Y-%m-%d %H:%M:%S')} (UTC)")
    except Exception as e:
        log(f"WARNING bump_next_login_run 失败: {e}")


SCHED_LOCK = threading.Lock()
SCHED_STARTED = False


def scheduled_login_job():
    # 由 APScheduler 触发的自动登录
    refresh_token_once(source="scheduler", bump_schedule=True)


def watchdog_job():
    """每分钟检查一次：如果已经过了 next_run_time 但登录任务没有真正开始执行，则立即补跑一次。"""
    try:
        job = scheduler.get_job(LOGIN_JOB_ID)
        if not job or not job.next_run_time:
            return

        expected_epoch = job.next_run_time.timestamp()
        now_epoch = time.time()
        mon = _get_mon()
        last_start = float(mon.get("last_login_start_epoch") or 0.0)

        # 超过 next_run_time + grace 仍未开始执行 -> 认为漏跑
        grace_sec = max(60, LOGIN_INTERVAL_MIN * 60)  # 宽限窗口：至少60秒，默认=登录间隔
        if now_epoch > expected_epoch + grace_sec and last_start < expected_epoch - 1:
            _set_mon(missed_count=int(mon.get("missed_count") or 0) + 1)
            log_sep("WATCHDOG")
            log("WARNING 检测到 自动自动登录可能未执行（MISFIRE），立即补跑一次登录 ...")
            log(f"INFO  now={datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | expected(UTC)={job.next_run_time.strftime('%Y-%m-%d %H:%M:%S')}")
            refresh_token_once(source="watchdog", bump_schedule=True)

    except Exception as e:
        log(f"ERROR watchdog_job 异常: {e}")


# =========================
# 20秒一次后端健康自检（对标 em10 逻辑）
# =========================
HEARTBEAT_JOB_ID = "health_heartbeat_20s"

def heartbeat_job():
    """每20秒请求一次自己的 /api/health，用于验证：进程/调度器/HTTP 都活着。"""
    base = HEALTH_BASE_URL or (_get_health().get("resolved_base_url") or "")
    if not base:
        # 还没拿到对外域名（例如服务刚起但还没人访问过）
        return
    url = base.rstrip("/") + HEALTH_PATH
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "em103-heartbeat/1.0"})
        ok = (r.status_code == 200)
        _set_health(
            last_heartbeat_epoch=time.time(),
            last_heartbeat_ok=ok,
            last_heartbeat_err="" if ok else f"http {r.status_code}",
        )
        # 只在异常时打日志，避免 20 秒刷屏
        if not ok:
            log(f"WARNING heartbeat http={r.status_code} url={url}")
    except Exception as e:
        _set_health(
            last_heartbeat_epoch=time.time(),
            last_heartbeat_ok=False,
            last_heartbeat_err=str(e),
        )
        log(f"WARNING heartbeat 异常: {e}")


def start_scheduler():
    global SCHED_STARTED
    with SCHED_LOCK:
        if SCHED_STARTED:
            return
        SCHED_STARTED = True

    # 1) 启动即先登录一次（建立 token + clubInfo 上下文）
    refresh_token_once(source="startup", bump_schedule=False)

    # 2) 每自动自动登录（next_run_time 初始按“现在+自动”）
    scheduler.add_job(
        scheduled_login_job,
        "interval",
        minutes=LOGIN_INTERVAL_MIN,
        id=LOGIN_JOB_ID,
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=LOGIN_INTERVAL_MIN),
    )

    # 3) Watchdog：每分钟检查一次是否漏跑
    scheduler.add_job(
        watchdog_job,
        "interval",
        seconds=60,
        id=WATCHDOG_JOB_ID,
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=60),
    )

    # 4) Heartbeat：每20秒自检一次（请求自己的 /api/health）
    scheduler.add_job(
        heartbeat_job,
        "interval",
        seconds=20,
        id=HEARTBEAT_JOB_ID,
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=20),
    )


    scheduler.start()
    bump_next_login_run(LOGIN_INTERVAL_MIN)

    log_sep("SCHEDULER")
    log("INFO  自动登录任务已启动：每 3 分钟刷新一次 token（以最后一次成功登录时间为准对齐 next_run_time）")
    log("INFO  Watchdog 已启用：若检测到漏跑，将立即补跑一次登录")


def ensure_scheduler_async():
    """在生产环境（尤其 gunicorn）避免 import 时启动调度器导致 fork/多进程问题。
    第一次有请求时再异步启动一次即可。"""
    if SCHED_STARTED:
        return
    t = threading.Thread(target=start_scheduler, daemon=True)
    t.start()


@app.before_request
def _ensure_scheduler_before_request():
    # 任何请求进来都确保调度器至少被尝试启动一次
    ensure_scheduler_async()



def get_next_login_epoch_ms():
    try:
        job = scheduler.get_job(LOGIN_JOB_ID)
        if not job or not job.next_run_time:
            return None
        return int(job.next_run_time.timestamp() * 1000)
    except Exception as e:
        log(f"WARNING get_next_login_epoch_ms 读取失败: {e}")
        return None


# =========================
# 前端 HTML（新增：showid 查询 + 缓存列表 + 一键解封）
# =========================
HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>HH🆚测试组🥰CMS 登录解封工具</title>
  <style>
    :root{
      --bg0:#070A12;
      --bg1:#0B1020;
      --card: rgba(255,255,255,.06);
      --border: rgba(255,255,255,.12);
      --text:#EAF0FF;
      --muted: rgba(234,240,255,.72);

      --good:#32FF9B;
      --bad:#FF4D6D;
      --warn:#FFB020;

      --shadow: 0 18px 60px rgba(0,0,0,.55);
      --shadow2: 0 10px 30px rgba(0,0,0,.35);
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono","Courier New", monospace;
      --tint0: rgba(7,10,18,.18);
      --tint1: rgba(11,16,32,.12);
    }

    html, body{
  background: transparent; /* ✅ 关键：不要用不透明背景盖住图片 */
}

body{
  margin: 0;
  padding: 22px;
  color: var(--text);
  font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial;
  min-height: 100vh;
  background: transparent; /* ✅ 关键 */
}

/* 背景图层：每次刷新随机切换（由 JS 设置 --bg-image） */
body::before{
  content:'';
  position: fixed;
  inset: 0;
  z-index: -3;
  background-image: var(--bg-image, none);
  background-size: cover;
  background-position: center center;
  background-repeat: no-repeat;
  transform: translateZ(0);
filter: brightness(1.10) saturate(1.02) contrast(1.02);
}

/* 可读性遮罩 + 氛围光晕（✅ 只用“半透明”叠加，不再用不透明 var(--bg0/bg1)） */
body::after{
  content:'';
  position: fixed;
  inset: 0;
  z-index: -2;
  background:
    radial-gradient(900px 500px at 20% 15%, rgba(108,168,255,.18), transparent 95%),
    radial-gradient(800px 520px at 85% 20%, rgba(50,255,155,.14), transparent 95%),
    radial-gradient(900px 600px at 40% 95%, rgba(255,77,109,.10), transparent 90%),
    linear-gradient(180deg, rgba(0,0,0,.10), rgba(0,0,0,.12)),
    linear-gradient(160deg, var(--tint0), var(--tint1)); /* ✅ 半透明色调 */
  pointer-events: none;
}

    .topbar{
      max-width: 1100px;
      margin: 0 auto 14px auto;
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap: 12px;
    }

    .brand{
      display:flex;
      align-items:center;
      gap: 10px;
    }
    .dot{
      width: 14px;
      height: 14px;
      border-radius: 999px;
      background: radial-gradient(circle at 30% 30%, rgba(255,255,255,.9), rgba(50,255,155,.9) 55%, rgba(50,255,155,.2));
      box-shadow: 0 0 18px rgba(50,255,155,.35);
    }
    .title{
      font-size: 18px;
      font-weight: 900;
      letter-spacing: .2px;
    }
    .clock{
      font-family: var(--mono);
      font-size: 13px;
      padding: 8px 10px;
      border-radius: 12px;
      background: rgba(255,255,255,.06);
      border: 1px solid var(--border);
      box-shadow: var(--shadow2);
      color: rgba(234,240,255,.85);
      display:flex;
      align-items:center;
      gap: 10px;
      white-space: nowrap;
    }
    .chip{
      display:inline-flex;
      align-items:center;
      gap: 8px;
      padding: 6px 10px;
      border-radius: 999px;
      background: rgba(255,255,255,.06);
      border: 1px solid rgba(255,255,255,.10);
      font-family: var(--mono);
      font-size: 12px;
      white-space: nowrap;
    }

    .card{
      max-width: 1100px;
      margin: 0 auto;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 16px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(12px);
    }

    .row{
      display:flex;
      align-items:center;
      gap: 10px;
      flex-wrap: wrap;
      margin-bottom: 12px;
    }
    .label{ color: var(--muted); font-weight: 700; }

    input{
      padding: 10px 12px;
      width: 300px;
      border-radius: 14px;
      border: 1px solid rgba(255,255,255,.14);
      background: rgba(0,0,0,.22);
      color: var(--text);
      outline: none;
      box-shadow: inset 0 0 0 1px rgba(0,0,0,.18);
    }
    input:focus{
      border-color: rgba(108,168,255,.55);
      box-shadow: 0 0 0 5px rgba(108,168,255,.14);
    }

    button{
      padding: 10px 14px;
      border-radius: 14px;
      border: 1px solid rgba(255,255,255,.14);
      background: rgba(255,255,255,.10);
      color: var(--text);
      cursor: pointer;
      font-weight: 800;
      letter-spacing: .2px;
      transition: transform .06s ease, background .15s ease, border-color .15s ease, box-shadow .15s ease;
    }
    button:hover{
      background: rgba(255,255,255,.14);
      border-color: rgba(255,255,255,.20);
      box-shadow: 0 10px 25px rgba(0,0,0,.25);
    }
    button:active{ transform: translateY(1px); }

    .btn-good{
      background: rgba(50,255,155,.12);
      border-color: rgba(50,255,155,.22);
    }
    .btn-good:hover{
      background: rgba(50,255,155,.18);
      border-color: rgba(50,255,155,.32);
      box-shadow: 0 0 0 6px rgba(50,255,155,.10), 0 12px 30px rgba(0,0,0,.35);
    }

    .btn-danger{
      background: rgba(255,77,109,.12);
      border-color: rgba(255,77,109,.22);
    }
    .btn-danger:hover{
      background: rgba(255,77,109,.18);
      border-color: rgba(255,77,109,.34);
      box-shadow: 0 0 0 6px rgba(255,77,109,.10), 0 12px 30px rgba(0,0,0,.35);
    }

    .btn-warn{
      background: rgba(255,176,32,.10);
      border-color: rgba(255,176,32,.22);
    }
    .btn-warn:hover{
      background: rgba(255,176,32,.14);
      border-color: rgba(255,176,32,.32);
    }

    .status-pill{
      display:inline-flex;
      align-items:center;
      gap: 10px;
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(255,255,255,.06);
      border: 1px solid rgba(255,255,255,.12);
      font-family: var(--mono);
      font-size: 12px;
      white-space: nowrap;
    }
    .pill-dot{
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: rgba(255,255,255,.25);
      box-shadow: 0 0 12px rgba(255,255,255,.16);
    }
    .pill-ok .pill-dot{
      background: rgba(50,255,155,.95);
      box-shadow: 0 0 18px rgba(50,255,155,.45);
    }
    .pill-bad .pill-dot{
      background: rgba(255,77,109,.95);
      box-shadow: 0 0 18px rgba(255,77,109,.45);
    }

    /* ===== Player Panel ===== */
    .section-title{
      margin: 14px 0 8px;
      font-weight: 900;
      letter-spacing: .2px;
      color: rgba(234,240,255,.90);
    }
    .hint{
      color: rgba(234,240,255,.68);
      font-size: 12px;
      margin-bottom: 8px;
    }
    .grid{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    @media (max-width: 900px){
      .grid{ grid-template-columns: 1fr; }
      input{ width: 100%; }
    }

    .player-card{
      border-radius: 16px;
      border: 1px solid rgba(255,255,255,.10);
      background: rgba(0,0,0,.26);
      box-shadow: inset 0 0 0 1px rgba(0,0,0,.18);
      padding: 12px;
      display:flex;
      gap: 12px;
      align-items:center;
    }
    .avatar{
      width: 54px;
      height: 54px;
      border-radius: 14px;
      overflow: hidden;
      border: 1px solid rgba(255,255,255,.10);
      background: rgba(255,255,255,.06);
      flex: 0 0 auto;
    }
    .avatar img{
      width: 100%;
      height: 100%;
      object-fit: cover;
      display:block;
    }
    .p-meta{
      flex: 1 1 auto;
      min-width: 0;
    }
    .p-nick{
      font-weight: 950;
      letter-spacing: .1px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .p-sub{
      margin-top: 3px;
      font-family: var(--mono);
      font-size: 12px;
      color: rgba(234,240,255,.78);
      display:flex;
      gap: 10px;
      flex-wrap: wrap;
    }
    .pill{
      display:inline-flex;
      align-items:center;
      gap: 6px;
      padding: 3px 8px;
      border-radius: 999px;
      border: 1px solid rgba(255,255,255,.10);
      background: rgba(255,255,255,.06);
      font-family: var(--mono);
      font-size: 12px;
      color: rgba(234,240,255,.82);
    }
    .p-actions{
      display:flex;
      gap: 8px;
      flex: 0 0 auto;
    }

    /* ===== Log viewer (colored lines) ===== */
    .log-wrap{
      width: 100%;
      border-radius: 16px;
      border: 1px solid rgba(255,255,255,.10);
      background:
        radial-gradient(800px 400px at 15% 10%, rgba(108,168,255,.06), transparent 90%),
        radial-gradient(700px 380px at 85% 25%, rgba(50,255,155,.05), transparent 90%),
        rgba(0,0,0,.28);
      box-shadow: inset 0 0 0 1px rgba(0,0,0,.20);
      overflow: hidden;
      margin-top: 14px;
    }

    .log-head{
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap: 10px;
      padding: 10px 12px;
      border-bottom: 1px solid rgba(255,255,255,.08);
      background: rgba(255,255,255,.04);
    }

    .log-title{
      font-family: var(--mono);
      font-size: 12px;
      color: rgba(234,240,255,.80);
    }

    .log-box{
      height: 520px;
      overflow:auto;
      padding: 12px;
      font-family: var(--mono);
      font-size: 12px;
      line-height: 1.55;
      white-space: pre-wrap;
      word-break: break-word;
    }

    .line{ color: rgba(234,240,255,.80); }
    .line.info{ color: rgba(234,240,255,.80); }
    .line.success{ color: rgba(50,255,155,.92); }
    .line.warn{ color: rgba(255,176,32,.92); }
    .line.error{ color: rgba(255,77,109,.92); }
    .line.sep{ color: rgba(234,240,255,.40); }

    /* ===== Toast（居中） ===== */
    .toast-wrap{
      position: fixed;
      left: 50%;
      top: 30%;
      transform: translate(-50%, -50%);
      z-index: 9999;
      display: flex;
      flex-direction: column;
      gap: 10px;
      pointer-events: none;
      align-items: center;
    }

    .toast{
      pointer-events: auto;
      min-width: 340px;
      max-width: 640px;
      padding: 12px 14px;
      border-radius: 16px;
      color: #fff;
      background: rgba(15,15,18,.92);
      border: 1px solid rgba(255,255,255,.14);
      backdrop-filter: blur(12px);
      box-shadow: 0 22px 70px rgba(0,0,0,.55);
      transform: translateY(-8px);
      opacity: 0;
      transition: all .18s ease;
      position: relative;
      overflow: hidden;
    }
    .toast.show{ transform: translateY(0); opacity: 1; }

    .toast.success{
      border-color: rgba(50,255,155,.40);
      box-shadow: 0 0 0 6px rgba(50,255,155,.10), 0 22px 70px rgba(0,0,0,.55);
    }
    .toast.error{
      border-color: rgba(255,77,109,.42);
      box-shadow: 0 0 0 6px rgba(255,77,109,.10), 0 22px 70px rgba(0,0,0,.55);
    }

    .toast .title{
      position: relative;
      font-weight: 950;
      margin-bottom: 8px;
      font-size: 14px;
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .toast .msg{
      position: relative;
      font-size: 13px;
      line-height: 1.35;
      opacity: .95;
      word-break: break-word;
    }

    .badge{
      display: inline-flex;
      align-items: center;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 900;
      background: rgba(255,255,255,.10);
      border: 1px solid rgba(255,255,255,.14);
      margin-right: 6px;
      font-family: var(--mono);
    }

    .toast .close{
      position:absolute;
      top: 8px;
      right: 10px;
      width: 26px;
      height: 26px;
      border-radius: 11px;
      border: 1px solid rgba(255,255,255,.14);
      background: rgba(255,255,255,.08);
      color: rgba(255,255,255,.9);
      cursor: pointer;
      display:flex;
      align-items:center;
      justify-content:center;
      line-height: 1;
      z-index: 1;
    }

    /* ===== Cache list scroll ===== */
    .cache-scroll{
      max-height: 420px;           /* ~3 cards */
      overflow-y: auto;
      padding-right: 6px;
      scrollbar-width: thin;       /* Firefox */
      scrollbar-color: rgba(255,255,255,.18) rgba(0,0,0,.15);
    }
    .cache-scroll::-webkit-scrollbar{ width: 10px; }
    .cache-scroll::-webkit-scrollbar-track{ background: rgba(0,0,0,.18); border-radius: 999px; }
    .cache-scroll::-webkit-scrollbar-thumb{ background: rgba(255,255,255,.18); border-radius: 999px; border: 2px solid rgba(0,0,0,.18); }

    /* ===== Theme toggle button ===== */
    .theme-btn{
      cursor: pointer;
      user-select: none;
      border: 1px solid rgba(255,255,255,.14);
      background: rgba(255,255,255,.06);
    }

    /* ===== Custom cursor ===== */
    #cursorDot{
      position: fixed;
      left: 0; top: 0;
      width: 16px; height: 16px;
      border-radius: 999px;
      border: 1px solid rgba(255,255,255,.25);
      background: rgba(255,255,255,.10);
      box-shadow: 0 0 18px rgba(108,168,255,.22);
      pointer-events: none;
      transform: translate(-50%,-50%);
      z-index: 9998;
      opacity: .0;
      transition: opacity .15s ease, width .15s ease, height .15s ease;
      mix-blend-mode: screen;
    }
    body:hover #cursorDot{ opacity: .85; }
    a:hover ~ #cursorDot, button:hover ~ #cursorDot { width: 20px; height: 20px; }

    /* ===== Falling FX (snow/petals) ===== */
    #fxLayer{
      position: fixed;
      inset: 0;
      pointer-events: none;
      overflow: hidden;
      z-index: 5;
    }
    .flake{
      position: absolute;
      top: -30px;
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: rgba(255,255,255,.65);
      filter: blur(.2px);
      opacity: .85;
      animation: fall linear forwards;
    }
    .petal{
      position: absolute;
      top: -40px;
      width: 14px;
      height: 10px;
      border-radius: 999px 999px 999px 0;
      background: rgba(255,170,200,.70);
      box-shadow: 0 6px 18px rgba(255,77,109,.10);
      opacity: .88;
      transform: rotate(25deg);
      animation: fallPetal linear forwards;
    }
    @keyframes fall{
      to { transform: translate3d(var(--dx), 110vh, 0) rotate(var(--rot)); opacity: 0; }
    }
    @keyframes fallPetal{
      50% { transform: translate3d(calc(var(--dx) * .6), 55vh, 0) rotate(calc(var(--rot) * .6)); }
      to  { transform: translate3d(var(--dx), 110vh, 0) rotate(var(--rot)); opacity: 0; }
    }
    @media (prefers-reduced-motion: reduce){
      #cursorDot, #fxLayer{ display:none !important; }
      *{ scroll-behavior: auto !important; }
    }

    /* ===== Mobile adaptation ===== */
    @media (max-width: 520px){
      body{ padding: 12px; }
      .topbar{ flex-direction: column; align-items: flex-start; gap: 10px; }
      .clock{ width: 100%; justify-content: space-between; }
      .chip{ width: 100%; justify-content: space-between; }
      .log-box{ height: 360px; }
      .cache-scroll{ max-height: 360px; }
      .player-card{ align-items: flex-start; }
      .p-actions{ flex-direction: column; }
    }

    /* ===== Theme palettes ===== */
    body[data-theme="midnight"]{
      --bg0:#050815;
      --bg1:#0A1030;
      --card: rgba(255,255,255,.06);
      --border: rgba(185,200,255,.16);
      /* ✅ 新增 */
  --tint0: rgba(5,8,21,.18);
  --tint1: rgba(10,16,48,.66);
    }
    body[data-theme="sakura"]{
      --bg0:#14070D;
      --bg1:#1A0B14;
      --card: rgba(255,255,255,.07);
      --border: rgba(255,170,200,.18);
      
  /* ✅ 新增 */
  --tint0: rgba(20,7,13,.18);
  --tint1: rgba(26,11,20,.66);
    }

</style>
</head>
<body>

  <div class="topbar">
    <div class="brand">
      <div class="dot"></div>
      <div class="title">HH@by测试组✅CMS 登录解封工具</div>
      <div class="chip" id="nextRunChip">next autologin: --</div>
      <button id="themeBtn" class="chip theme-btn" onclick="toggleTheme()">theme: dark</button>
    </div>

    <div class="clock">
      <span>🕒</span>
      <span id="nowClock">--</span>
    </div>
  </div>

  <div class="card">
    <div class="row">
      <span class="label">登录状态：</span>
      <span id="st" class="status-pill"><span class="pill-dot"></span><span>loading...</span></span>
      <button class="btn-good" onclick="loginNow()">立即登录一次</button>
    </div>

    <div class="section-title">玩家查询（showid → uuid/昵称/头像）</div>
    <div class="hint">查询成功后会自动加入缓存；查询/解封都复用同一套 token + clubInfo 上下文逻辑。</div>

    <div class="row">
      <span class="label" style="color: #ff4d6d;">输入 showid：</span>
      <input id="showidSearch" placeholder="例如 10518356534" value="10198130419" />
      <button class="btn-good" onclick="lookupUser()">查询uuid</button>
      <button class="btn-danger" onclick="clearUserCache()">清空资料缓存</button>
    </div>

    <div class="grid">
      <div>
        <div class="section-title" style="margin-top:0;">查询结果</div>
        <div id="searchResult"></div>
      </div>

      <div>
        <div class="section-title" style="margin-top:0;">查询后自动缓存列表（点击可选择/一键解封CMS）</div>
        <div id="cacheListWrap" class="cache-scroll"><div id="cacheList"></div></div>
      </div>
    </div>

    <div class="section-title">解封</div>
    <div class="row">
      <span class="label">showid：</span>
      <input id="showidUnlock" placeholder="例如 10198130419"/>
      <button class="btn-good" onclick="unlock()">发送解封请求</button>
    </div>

    <div class="log-wrap">
      <div class="log-head">
        <div class="log-title">日志（最新在上）</div>
        <button class="btn-danger" onclick="clearLogs()">清空日志</button>
      </div>
      <div id="logBox" class="log-box"></div>
    </div>
  </div>

  <div id="toastWrap" class="toast-wrap"></div>

<script>
let nextLoginEpochMs = null;

const DEFAULT_SHOWID = '10198130419';
let defaultCacheTried = false;

async function ensureDefaultCached(){
  if(defaultCacheTried) return;
  defaultCacheTried = true;

  const form = new URLSearchParams();
  form.append('showid', DEFAULT_SHOWID);

  try{
    // silently try to cache default user once
    const r = await fetch('/api/user_lookup', {
      method:'POST',
      headers:{'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8'},
      body: form.toString()
    });
    const j = await r.json();
    if(j && j.ok){
      // refresh cache list after caching
      await refreshUserCache();
      await refreshStatus();
    }
  }catch(_e){}
}


function pad2(n){ return String(n).padStart(2,'0'); }

function fmtHMS(sec){
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return `${pad2(h)}:${pad2(m)}:${pad2(s)}`;
}

function fmtYMDHMS(ms){
  const d = new Date(ms);
  return `${d.getFullYear()}-${pad2(d.getMonth()+1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}

function escapeHtml(s){
  return String(s ?? '').replace(/[&<>"']/g, m => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[m]));
}

function showToast({ type = 'success', title = '', msg = '', duration = 2600 }){
  const wrap = document.getElementById('toastWrap');
  const el = document.createElement('div');
  el.className = `toast ${type}`;

  el.innerHTML = `
    <button class="close" aria-label="close">×</button>
    <div class="title">${escapeHtml(title)}</div>
    <div class="msg">${msg}</div>
  `;

  wrap.appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));

  const remove = () => {
    el.classList.remove('show');
    setTimeout(() => el.remove(), 180);
  };
  el.querySelector('.close').addEventListener('click', remove);
  setTimeout(remove, duration);
}

function classifyLine(line){
  const s = line || '';
  if (s.startsWith("──") || s.startsWith("【")) return "sep";
  if (s.includes("ERROR") || s.includes("失败") || s.includes("异常")) return "error";
  if (s.includes("WARNING") || s.includes("⚠️")) return "warn";
  if (s.includes("SUCCESS") || s.includes("成功") || s.includes("一致 ✅") || s.includes("iErrCode=0")) return "success";
  if (s.includes("iErrCode") && !s.includes("iErrCode=0")) return "error";
  return "info";
}

function renderPlayerCard(p, {showSelect=true, showUnlock=true}={}){
  if(!p) return '';
  const cover = p.strCover ? escapeHtml(p.strCover) : '';
  const nick = escapeHtml(p.strNick || '-');
  const showid = escapeHtml(p.showid || '-');
  const uuid = escapeHtml(p.uuid ?? 'N/A');
  const cachedAt = p.cached_at ? `<span class="pill">cached: ${escapeHtml(p.cached_at)}</span>` : '';

 return `
  <div class="player-card">
    <div class="avatar">${cover ? `<img src="${cover}" />` : ''}</div>
    <div class="p-meta">
      <div class="p-nick">${nick}</div>
      <div class="p-sub">
        <span class="pill">
          <span style="color:rgba(108,168,255,.95); font-weight:900;">showid:</span>
          <span style="font-weight:950; font-size:14px; color:rgba(234,240,255,.98);">${showid}</span>
        </span>
        <span class="pill">
          <span style="color:rgba(50,255,155,.92); font-weight:900;">uuid:</span>
          <span style="font-weight:950; font-size:14px; color:rgba(234,240,255,.98);">${uuid}</span>
        </span>
        ${cachedAt}
      </div>
    </div>
    <div class="p-actions">
      ${showSelect ? `<button class="btn-warn" onclick="selectCached('${showid}')">选择</button>` : ``}
      ${showUnlock ? `<button class="btn-good" onclick="unlockDirect('${showid}')">一键解封CMS</button>` : ``}
    </div>
  </div>`;

}

async function refreshStatus(){
  const r = await fetch('/api/status?t=' + Date.now(), { cache: 'no-store' });
  const j = await r.json();

  nextLoginEpochMs = j.next_login_epoch_ms;

  const st = document.getElementById('st');
  const dot = `<span class="pill-dot"></span>`;
  if(j.last_login_ok){
    st.className = 'status-pill pill-ok';
    st.innerHTML = `${dot}<span>已登录 | 最近登录: ${escapeHtml(j.last_login_at || '-')} | token: ${j.has_token ? '是' : '否'} | clubCtx: ${j.clubctx_ok ? 'OK' : 'NO'} | cache: ${j.user_cache_count}</span>`;
  }else{
    st.className = 'status-pill pill-bad';
    st.innerHTML = `${dot}<span>未登录/失败 | ${escapeHtml(j.last_login_err || 'no token')} | clubCtx: ${j.clubctx_ok ? 'OK' : 'NO'} | cache: ${j.user_cache_count}</span>`;
  }
}

async function refreshLogs(){
  const r = await fetch('/api/logs?t=' + Date.now(), { cache: 'no-store' });
  const j = await r.json();
  const box = document.getElementById('logBox');
  const lines = j.lines || [];

  const html = lines.map(line => {
    const cls = classifyLine(line);
    const safe = (line === '') ? '&nbsp;' : escapeHtml(line);
    return `<div class="line ${cls}">${safe}</div>`;
  }).join('');
  box.innerHTML = html;
}

async function clearLogs(){
  await fetch('/api/logs/clear', {method:'POST'});
  await refreshLogs();
  showToast({ type:'success', title:'已清空日志', msg:'日志已清空。', duration: 1800 });
}

async function loginNow(){
  try{
    showToast({ type:'success', title:'登录中', msg:'正在执行立即登录...', duration: 1400 });
    const r = await fetch('/api/login_now', {method:'POST'});
    const j = await r.json();
    if(j.ok){
      showToast({
        type:'success',
        title:'登录成功',
        msg:`<span class="badge">time</span> ${escapeHtml(j.last_login_at || '-')}`,
        duration: 2200
      });
    }else{
      showToast({
        type:'error',
        title:'登录失败',
        msg: escapeHtml(j.msg || 'unknown error'),
        duration: 5200
      });
    }
    await refreshStatus();
    await refreshLogs();
  }catch(e){
    showToast({ type:'error', title:'请求异常', msg: escapeHtml(e?.message || String(e)), duration: 5200 });
  }
}

async function lookupUser(){
  const showid = document.getElementById('showidSearch').value.trim();
  if(!showid){
    showToast({ type:'error', title:'参数错误', msg:'请输入 showid', duration: 2400 });
    return;
  }

  const form = new URLSearchParams();
  form.append('showid', showid);

  try{
    showToast({ type:'success', title:'查询中', msg:`showid=${escapeHtml(showid)}`, duration: 1200 });

    const r = await fetch('/api/user_lookup', {
      method:'POST',
      headers:{'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8'},
      body: form.toString()
    });

    const j = await r.json();
    if(!j.ok){
      showToast({ type:'error', title:'查询失败', msg: escapeHtml(j.msg || 'unknown'), duration: 5200 });
      document.getElementById('searchResult').innerHTML = '';
      await refreshLogs();
      return;
    }

    // 渲染查询结果
    const p = j.profile;
    document.getElementById('searchResult').innerHTML = renderPlayerCard(p, {showSelect:true, showUnlock:true});

    // 刷新缓存列表
    await refreshUserCache();
    await refreshStatus();
    await refreshLogs();

    showToast({
      type:'success',
      title:'查询成功',
      msg:`<span class="badge">nick</span> ${escapeHtml(p.strNick)} <span class="badge">uuid</span> ${escapeHtml(p.uuid)}`,
      duration: 2400
    });

  }catch(e){
    showToast({ type:'error', title:'请求异常', msg: escapeHtml(e?.message || String(e)), duration: 5200 });
  }
}

async function refreshUserCache(){
  const r = await fetch('/api/user_cache?t=' + Date.now(), { cache: 'no-store' });
  const j = await r.json();
  const list = j.items || [];
  const box = document.getElementById('cacheList');
  if(list.length === 0){
    box.innerHTML = `<div class="hint">暂无缓存，正在自动缓存默认用户 <b>${DEFAULT_SHOWID}</b> ...</div>`;
    await ensureDefaultCached();
    // ensureDefaultCached will refresh list on success; if still empty, keep hint
    return;
  }
  box.innerHTML = list.map(p => renderPlayerCard(p, {showSelect:true, showUnlock:true})).join('');
}

async function clearUserCache(){
  await fetch('/api/user_cache/clear', {method:'POST'});
  await refreshUserCache();
  await refreshStatus();
  showToast({ type:'success', title:'已清空缓存', msg:'玩家资料缓存已清空。', duration: 1800 });
}

function selectCached(showid){
  document.getElementById('showidUnlock').value = showid;
  showToast({ type:'success', title:'已选择', msg:`已填入解封 showid=${escapeHtml(showid)}`, duration: 1600 });
}

function normalizeResponseToObj(resp){
  if(resp && typeof resp === 'object') return resp;
  if(typeof resp === 'string'){
    try { return JSON.parse(resp); } catch (_) { return null; }
  }
  return null;
}

async function unlock(){
  const showid = document.getElementById('showidUnlock').value.trim();
  if(!showid){
    showToast({ type:'error', title:'参数错误', msg:'请输入 showid', duration: 2400 });
    return;
  }
  await unlockDirect(showid);
}

async function unlockDirect(showid){
  const form = new URLSearchParams();
  form.append('showid', showid);

  try{
    const r = await fetch('/unlock_club_manager', {
      method:'POST',
      headers:{'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8'},
      body: form.toString()
    });

    const j = await r.json();
    const bodyObj = normalizeResponseToObj(j.response);
    const iErrCode = bodyObj?.iErrCode;

    // ✅ 成功判定：status=200 且 iErrCode=0
    const ok = (j.status_code === 200) && (iErrCode === 0);

    const respText = typeof j.response === 'string' ? j.response : JSON.stringify(j.response);
    const summaryRaw = (respText || '').slice(0, 260);
    const summary = escapeHtml(summaryRaw) + ((respText || '').length > 260 ? '…' : '');

    showToast({
      type: ok ? 'success' : 'error',
      title: ok ? '✅ 解封成功 ✅' : '❌ 解封失败 ❌',
      msg: `
        <div style="margin-bottom:30px;">
          <span class="badge">showid: ${escapeHtml(showid)}</span>
          <span class="badge">status: ${escapeHtml(j.status_code)}</span>
          <span class="badge">iErrCode: ${escapeHtml(iErrCode ?? 'N/A')}</span>
        </div>
        <div style="opacity:.95;">${summary || '无返回内容'}</div>
      `,
      duration: ok ? 2600 : 5600
    });

    await refreshStatus();
    await refreshLogs();
  }catch(e){
    showToast({ type:'error', title:'请求异常', msg: escapeHtml(e?.message || String(e)), duration: 5200 });
  }
}


// ===== Theme + FX =====
const THEMES = ['dark', 'midnight', 'sakura'];

function applyTheme(theme){
  if(!theme) theme = 'dark';
  if(theme === 'dark'){
    document.body.removeAttribute('data-theme');
  }else{
    document.body.setAttribute('data-theme', theme);
  }
  const btn = document.getElementById('themeBtn');
  if(btn) btn.textContent = `theme: ${theme}`;
  try{ localStorage.setItem('cms_theme', theme); }catch(_e){}
}


// ===== Random Background (local cached via Service Worker + Cache-Control) =====
async function initBackground(){
  try{
    // 注册 Service Worker（用于预缓存背景图到浏览器本地）
    if('serviceWorker' in navigator){
      try{
        await navigator.serviceWorker.register('/sw.js', { scope: '/' });
      }catch(_e){}
    }

    const r = await fetch('/api/backgrounds?t=' + Date.now(), { cache: 'no-store' });
    const j = await r.json();
    const items = (j && j.items) ? j.items : [];
    if(!items.length){
      // 没有背景图就保持默认渐变
      return;
    }

    // 每次刷新随机：尽量避开上一次背景
    let last = null;
    try{ last = localStorage.getItem('cms_bg_last') || null; }catch(_e){}
    let pick = items[Math.floor(Math.random() * items.length)];
    if(items.length > 1 && last && pick === last){
      // reroll once
      pick = items[Math.floor(Math.random() * items.length)];
    }
    document.body.style.setProperty('--bg-image', `url("${pick}")`);
    try{ localStorage.setItem('cms_bg_last', pick); }catch(_e){}
  }catch(_e){
    // ignore
  }
}

function toggleTheme(){
  let cur = 'dark';
  try{ cur = localStorage.getItem('cms_theme') || 'dark'; }catch(_e){}
  const i = THEMES.indexOf(cur);
  const next = THEMES[(i < 0 ? 0 : (i + 1) % THEMES.length)];
  applyTheme(next);
  // change FX flavor immediately
  restartFx();
}

let fxTimer = null;
function spawnFxOne(){
  const layer = document.getElementById('fxLayer');
  if(!layer) return;

  const theme = (function(){ try{ return localStorage.getItem('cms_theme') || 'dark'; }catch(_e){ return 'dark'; } })();
  const isSakura = theme === 'sakura';

  const el = document.createElement('div');
  el.className = isSakura ? 'petal' : 'flake';

  const left = Math.random() * 100;         // vw
  const dur  = 6 + Math.random() * 6;       // seconds
  const dx   = (Math.random() * 160 - 80) + 'px';
  const rot  = (Math.random() * 720 - 360) + 'deg';
  const scale = 0.7 + Math.random() * 0.9;

  el.style.left = left + 'vw';
  el.style.animationDuration = dur + 's';
  el.style.setProperty('--dx', dx);
  el.style.setProperty('--rot', rot);
  el.style.transform = `scale(${scale}) rotate(${rot})`;

  // slightly vary size
  if(isSakura){
    el.style.width = (10 + Math.random() * 10) + 'px';
    el.style.height = (7 + Math.random() * 8) + 'px';
    el.style.opacity = (0.65 + Math.random() * 0.35).toFixed(2);
  }else{
    const s = 6 + Math.random() * 10;
    el.style.width = s + 'px';
    el.style.height = s + 'px';
    el.style.opacity = (0.55 + Math.random() * 0.35).toFixed(2);
  }

  layer.appendChild(el);
  setTimeout(() => el.remove(), (dur + 1) * 1000);
}

function startFx(){
  // disable on touch / coarse pointer
  const fine = window.matchMedia && window.matchMedia('(pointer:fine)').matches;
  if(!fine) return;
  if(fxTimer) return;
  fxTimer = setInterval(spawnFxOne, 450); // density
}

function stopFx(){
  if(fxTimer){
    clearInterval(fxTimer);
    fxTimer = null;
  }
  const layer = document.getElementById('fxLayer');
  if(layer) layer.innerHTML = '';
}

function restartFx(){
  stopFx();
  startFx();
}

// Custom cursor dot
(function initCursor(){
  const fine = window.matchMedia && window.matchMedia('(pointer:fine)').matches;
  if(!fine) return;

  const dot = document.getElementById('cursorDot');
  if(!dot) return;

  let x = -100, y = -100;
  let tx = x, ty = y;

  window.addEventListener('mousemove', (e) => {
    tx = e.clientX;
    ty = e.clientY;
  }, {passive:true});

  window.addEventListener('mouseleave', () => {
    dot.style.opacity = '0';
  });

  window.addEventListener('mouseenter', () => {
    dot.style.opacity = '.85';
  });

  function raf(){
    x += (tx - x) * 0.18;
    y += (ty - y) * 0.18;
    dot.style.left = x + 'px';
    dot.style.top  = y + 'px';
    requestAnimationFrame(raf);
  }
  raf();
})();

function tickClockAndCountdown(){
  // 右上角：年月日 + 时分秒（秒级）
  const d = new Date();
  document.getElementById('nowClock').textContent =
    `${d.getFullYear()}-${pad2(d.getMonth()+1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;

  // next autologin：绝对时间 + 倒计时
  const chip = document.getElementById('nextRunChip');
  if(!nextLoginEpochMs){
    chip.textContent = 'next autologin: --';
    return;
  }
  const nowMs = Date.now();
  const remainSec = Math.max(0, Math.floor((nextLoginEpochMs - nowMs) / 1000));
  chip.textContent = `next autologin: ${fmtYMDHMS(nextLoginEpochMs)} (in ${fmtHMS(remainSec)})`;
}

// 定时刷新：状态/日志/缓存（缓存刷新频率不需要太高）
setInterval(tickClockAndCountdown, 1000);
setInterval(async ()=>{ await refreshStatus(); await refreshLogs(); }, 2500);
setInterval(async ()=>{ await refreshUserCache(); }, 6000);
setInterval(()=>{ fetch('/api/health', {cache:'no-store'}).catch(()=>{}); }, 20000);

applyTheme((()=>{ try{ return localStorage.getItem('cms_theme') || 'dark'; }catch(_e){ return 'dark'; } })());
restartFx();
initBackground();
refreshStatus(); refreshLogs(); refreshUserCache(); tickClockAndCountdown();
</script>
  <div id="fxLayer"></div>
  <div id="cursorDot"></div>
</body>
</html>
"""


# =========================
# Routes
# =========================
@app.get("/")
def home():
    return render_template_string(HTML)


@app.get("/api/status")
def api_status():
    st = get_status_snapshot()
    ctx = get_clubctx()
    next_ms = get_next_login_epoch_ms()
    mon = _get_mon()
    return jsonify({
        "last_login_ok": st["last_login_ok"],
        "last_login_at": st["last_login_at"],
        "last_login_err": st["last_login_err"],
        "has_token": bool(st["token"]),
        "server_epoch_ms": int(time.time() * 1000),
        "server_time_utc": datetime.now(timezone.utc).isoformat(),
        "server_time_local": datetime.now().isoformat(),
        "server_epoch_ms": int(time.time() * 1000),
        "server_tzname": time.tzname,
        "server_time_local": datetime.now().isoformat(),
        "server_tzname": time.tzname,
        "next_login_epoch_ms": next_ms,
        "next_login_time_utc": (scheduler.get_job(LOGIN_JOB_ID).next_run_time.isoformat() if scheduler.get_job(LOGIN_JOB_ID) and scheduler.get_job(LOGIN_JOB_ID).next_run_time else None),
        "scheduler_running": bool(getattr(scheduler, "running", False)),
        "clubctx_ok": bool(ctx.get("ok")),
        "clubctx_last_at": ctx.get("last_at"),
        "clubctx_last_err": ctx.get("last_err"),
        "user_cache_count": cache_count(),
        "last_login_source": mon.get("last_login_source"),
        "missed_count": mon.get("missed_count"),
    })

@app.get("/api/health")
def api_health():
    # 动态补全域名（如果没设置 HEALTH_BASE_URL/RENDER_EXTERNAL_URL）
    base = resolve_base_url_from_request()

    st = get_status_snapshot()
    ctx = get_clubctx()
    next_ms = get_next_login_epoch_ms()
    mon = _get_mon()
    hs = _get_health()

    payload = {
        "status": "ok",
        "server_time_utc": datetime.now(timezone.utc).isoformat(),
        "server_time_local": datetime.now().isoformat(),
        "server_epoch_ms": int(time.time() * 1000),
        "server_tzname": time.tzname,
        "base_url": base,
        "health_url": (base + HEALTH_PATH) if base else HEALTH_PATH,
        "scheduler_running": scheduler.running if scheduler else False,
        "next_login_epoch_ms": next_ms,
        "next_login_time_utc": (scheduler.get_job(LOGIN_JOB_ID).next_run_time.isoformat() if scheduler.get_job(LOGIN_JOB_ID) and scheduler.get_job(LOGIN_JOB_ID).next_run_time else None),
        "last_login_ok": st.get("last_login_ok"),
        "last_login_at": st.get("last_login_at"),
        "last_login_err": st.get("last_login_err"),
        "clubctx_ok": ctx.get("ok"),
        "clubctx_last_at": ctx.get("last_at"),
        "clubctx_last_err": ctx.get("last_err"),
        # 心跳（20秒自检）
        "heartbeat_last_epoch": hs.get("last_heartbeat_epoch"),
        "heartbeat_last_ok": hs.get("last_heartbeat_ok"),
        "heartbeat_last_err": hs.get("last_heartbeat_err"),
        # 登录监控
        "login_last_start_epoch": mon.get("last_login_start_epoch"),
        "login_last_end_epoch": mon.get("last_login_end_epoch"),
        "login_last_source": mon.get("last_login_source"),
        "login_missed_count": mon.get("missed_count"),
    }
    return jsonify(payload)


@app.get("/api/logs")
def api_logs():
    with LOG_LOCK:
        return jsonify({"lines": list(LOG_BUF)})


@app.post("/api/logs/clear")
def api_logs_clear():
    clear_logs()
    log("INFO  日志已清空（用户操作）")
    return jsonify({"ok": True})


@app.post("/api/login_now")
def api_login_now():
    ok, msg = refresh_token_once()
    st = get_status_snapshot()
    return jsonify({
        "ok": ok,
        "msg": msg,
        "last_login_at": st["last_login_at"],
        "has_token": bool(st["token"]),
    })


# =========================
# 玩家查询（showid -> uuid/昵称/头像）并缓存
# =========================
@app.post("/api/user_lookup")
def api_user_lookup():
    showid = (request.form.get("showid") or "").strip()
    if not showid:
        return jsonify({"ok": False, "msg": "showid required"}), 400

    # 统一校验 token + 上下文
    ok, msg = ensure_auth_and_context()
    if not ok:
        return jsonify({"ok": False, "msg": f"auth/context not ready: {msg}"}), 503

    token = get_token()
    if not token:
        return jsonify({"ok": False, "msg": "no token cached"}), 503

    try:
        j = fetch_user_by_showid(showid, token)

        # 期望格式：{"iErrCode":0, "result": {...}}
        if not isinstance(j, dict):
            return jsonify({"ok": False, "msg": "bad response type", "raw": j}), 502

        if j.get("iErrCode") != 0:
            return jsonify({"ok": False, "msg": f"iErrCode={j.get('iErrCode')}", "raw": j}), 200

        result = j.get("result") or {}
        profile = {
            "showid": str(result.get("sShowID") or showid),
            "uuid": result.get("uuid"),
            "strNick": result.get("strNick") or "",
            "strCover": result.get("strCover") or "",
        }

        cache_user(profile)

        log_sep("USER LOOKUP PARSED")
        log(f"SUCCESS 查询解析成功: showid={profile['showid']} uuid={profile['uuid']} nick={profile['strNick']}")
        log(f"INFO  cover={profile['strCover']}")
        log(f"INFO  已写入缓存（cache_count={cache_count()}）")

        # 回传给前端（带 cached_at）
        cached_items = list_cached_users()
        cached_one = next((x for x in cached_items if x["showid"] == profile["showid"]), None)
        return jsonify({"ok": True, "profile": (cached_one or profile)})

    except Exception as e:
        log_sep("USER LOOKUP FAILED")
        log(f"ERROR 玩家查询异常: {e}")
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.get("/api/user_cache")
def api_user_cache():
    return jsonify({"items": list_cached_users()})


@app.post("/api/user_cache/clear")
def api_user_cache_clear():
    clear_user_cache()
    log("INFO  玩家资料缓存已清空（用户操作）")
    return jsonify({"ok": True})


# =========================
# 解封接口：结构保持固定（你要求的格式）
# 如果上下文未建立：自动重走登录流程（含 clubInfo）后再解封
# =========================
@app.route("/unlock_club_manager", methods=["POST"])
def unlock_club_manager():
    showid = (request.form.get("showid") or "").strip()
    if not showid:
        return jsonify({"error": "showid required"}), 400

    # 统一校验 token + 上下文
    ok, msg = ensure_auth_and_context()
    if not ok:
        return jsonify({"error": "club context not ready", "detail": msg}), 503

    token = get_token()
    if not token:
        return jsonify({"error": "no token cached"}), 503

    headers = {
        "accept": "application/json, text/javascript, */*; q=0.01",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "token": token,
        "referer": CMS_REFERER
    }
    data = {"showid": showid}

    log_sep("UNLOCK REQUEST")
    log(f"INFO  发送解封请求：showid={showid}")
    log("INFO  解封请求 token（完整如下）")
    log(token)

    r = requests.post(CMS_UNLOCK_URL, headers=headers, data=data, timeout=10)
    log(f"INFO  解封响应：status={r.status_code}")
    log(f"INFO  解封 body: {r.text}")

    return jsonify({
        "status_code": r.status_code,
        "response": r.json() if "application/json" in r.headers.get("content-type", "") else r.text
    })


# =========================
# Background assets + Service Worker
# =========================
@app.get("/api/backgrounds")
def api_backgrounds():
    files = list_bg_files()
    return jsonify({
        "count": len(files),
        "items": [bg_url_for(x) for x in files],
    })


@app.get("/bg/<path:filename>")
def serve_bg(filename):
    # 安全限制：只允许 BG_DIR 里的文件名
    filename = os.path.basename(filename)
    if not filename:
        return ("bad filename", 400)
    full = os.path.join(BG_DIR, filename)
    if not os.path.isfile(full):
        return ("not found", 404)
    resp = make_response(send_from_directory(BG_DIR, filename))
    # 30天强缓存，配合 ?v=mtime 实现“更新即生效”
    resp.headers["Cache-Control"] = "public, max-age=2592000, immutable"
    return resp


@app.get("/sw.js")
def service_worker():
    # 生成一个简单 SW：预缓存背景图 + 静态资源；图片 cache-first
    files = list_bg_files()
    bg_urls = [bg_url_for(x) for x in files]
    # 让 SW 版本在背景图列表变更时变化（用于触发更新）
    version_seed = "|".join(files)
    ver = str(abs(hash(version_seed)) % (10 ** 10))

    sw = f"""
// Auto-generated by Flask
const CACHE_NAME = 'cms-bg-cache-v{ver}';
const PRECACHE_URLS = {bg_urls!r}.concat(['/','/api/status','/api/logs','/api/user_cache']);

self.addEventListener('install', (event) => {{
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(PRECACHE_URLS)).then(() => self.skipWaiting())
  );
}});

self.addEventListener('activate', (event) => {{
  event.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(k => k.startsWith('cms-bg-cache-') && k !== CACHE_NAME).map(k => caches.delete(k))
    )).then(() => self.clients.claim())
  );
}});

self.addEventListener('fetch', (event) => {{
  const req = event.request;
  const url = new URL(req.url);

  // 只处理同源
  if (url.origin !== self.location.origin) return;

  // 背景图：cache-first
  if (url.pathname.startsWith('/bg/')) {{
    event.respondWith(
      caches.open(CACHE_NAME).then(cache =>
        cache.match(req).then(hit => hit || fetch(req).then(res => {{
          if(res && res.status === 200) cache.put(req, res.clone());
          return res;
        }}))
      )
    );
    return;
  }}

  // 其他：stale-while-revalidate（更快）
  event.respondWith(
    caches.open(CACHE_NAME).then(cache =>
      cache.match(req).then(hit => {{
        const fetchPromise = fetch(req).then(res => {{
          if(res && res.status === 200 && req.method === 'GET') cache.put(req, res.clone());
          return res;
        }}).catch(_ => hit);
        return hit || fetchPromise;
      }})
    )
  );
}});
"""
    resp = make_response(sw)
    resp.headers["Content-Type"] = "application/javascript; charset=utf-8"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5013"))
    app.run(host="0.0.0.0", port=port, debug=False)
